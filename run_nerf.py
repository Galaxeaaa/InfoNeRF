import os, sys
import numpy as np
import imageio
import json
import random
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm, trange

import matplotlib.pyplot as plt

from run_nerf_helpers import *

from dataset.load_llff import load_llff_data
from dataset.load_dtu import load_dtu_data
from dataset.load_blender import load_blender_data
from dataset.load_oppo import load_oppo_data

from utils.loss import EntropyLoss, SmoothingLoss
from utils.generate_near_c2w import GetNearC2W, get_near_pixel

from data import RayDataset
from torch.utils.data import DataLoader

from utils.generate_renderpath import generate_renderpath
import cv2
import random
import wandb
import torchvision


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
np.random.seed(0)
torch.manual_seed(0)
random.seed(0)

DEBUG = False

def batchify(fn, chunk):
    """Constructs a version of 'fn' that applies to smaller batches.
    """
    if chunk is None:
        return fn
    def ret(inputs):
        return torch.cat([fn(inputs[i:i+chunk]) for i in range(0, inputs.shape[0], chunk)], 0)
    return ret


def run_network(inputs, viewdirs, fn, embed_fn, embeddirs_fn, netchunk=1024*64):
    """Prepares inputs and applies network 'fn'.
    """
    inputs_flat = torch.reshape(inputs, [-1, inputs.shape[-1]])
    embedded = embed_fn(inputs_flat)

    if viewdirs is not None:
        input_dirs = viewdirs[:,None].expand(inputs.shape)
        input_dirs_flat = torch.reshape(input_dirs, [-1, input_dirs.shape[-1]])
        embedded_dirs = embeddirs_fn(input_dirs_flat)
        embedded = torch.cat([embedded, embedded_dirs], -1)

    outputs_flat = batchify(fn, netchunk)(embedded)
    outputs = torch.reshape(outputs_flat, list(inputs.shape[:-1]) + [outputs_flat.shape[-1]])
    return outputs


def batchify_rays(rays_flat, chunk=1024*32, **kwargs):
    """Render rays in smaller minibatches to avoid OOM.
    """
    all_ret = {}
    for i in range(0, rays_flat.shape[0], chunk):
        ret = render_rays(rays_flat[i:i+chunk], **kwargs)
        for k in ret:
            if k not in all_ret:
                all_ret[k] = []
            all_ret[k].append(ret[k])

    all_ret = {k : torch.cat(all_ret[k], 0) for k in all_ret}
    return all_ret


def render(H, W, focal, chunk=1024*32, rays=None, c2w=None, ndc=True,
                  near=0., far=1.,
                  use_viewdirs=False, c2w_staticcam=None, depths=None,
                  **kwargs):
    """Render rays
    Args:
      H: int. Height of image in pixels.
      W: int. Width of image in pixels.
      focal: float. Focal length of pinhole camera.
      chunk: int. Maximum number of rays to process simultaneously. Used to
        control maximum memory usage. Does not affect final results.
      rays: array of shape [2, batch_size, 3]. Ray origin and direction for
        each example in batch.
      c2w: array of shape [3, 4]. Camera-to-world transformation matrix.
      ndc: bool. If True, represent ray origin, direction in NDC coordinates.
      near: float or array of shape [batch_size]. Nearest distance for a ray.
      far: float or array of shape [batch_size]. Farthest distance for a ray.
      use_viewdirs: bool. If True, use viewing direction of a point in space in model.
      c2w_staticcam: array of shape [3, 4]. If not None, use this transformation matrix for 
       camera while using other c2w argument for viewing directions.
    Returns:
      rgb_map: [batch_size, 3]. Predicted RGB values for rays.
      disp_map: [batch_size]. Disparity map. Inverse of depth.
      acc_map: [batch_size]. Accumulated opacity (alpha) along a ray.
      extras: dict with everything returned by render_rays().
    """
    if c2w is not None:
        # special case to render full image
        rays_o, rays_d = get_rays(H, W, focal, c2w)
    else:
        # use provided ray batch
        rays_o, rays_d = rays

    if use_viewdirs:
        # provide ray directions as input
        viewdirs = rays_d
        if c2w_staticcam is not None:
            # special case to visualize effect of viewdirs
            rays_o, rays_d = get_rays(H, W, focal, c2w_staticcam)
        viewdirs = viewdirs / torch.norm(viewdirs, dim=-1, keepdim=True)
        viewdirs = torch.reshape(viewdirs, [-1,3]).float()

    sh = rays_d.shape # [..., 3]
    if ndc:
        # for forward facing scenes
        rays_o, rays_d = ndc_rays(H, W, focal, 1., rays_o, rays_d)

    # Create ray batch
    rays_o = torch.reshape(rays_o, [-1,3]).float()
    rays_d = torch.reshape(rays_d, [-1,3]).float()

    near, far = near * torch.ones_like(rays_d[...,:1]), far * torch.ones_like(rays_d[...,:1])
    rays = torch.cat([rays_o, rays_d, near, far], -1) # B x 8
    if depths is not None:
        rays = torch.cat([rays, depths.reshape(-1,1)], -1)
    if use_viewdirs:
        rays = torch.cat([rays, viewdirs], -1)
    # Render and reshape
    all_ret = batchify_rays(rays, chunk, **kwargs)
    for k in all_ret:
        k_sh = list(sh[:-1]) + list(all_ret[k].shape[1:])
        all_ret[k] = torch.reshape(all_ret[k], k_sh)

    k_extract = ['rgb_map', 'disp_map', 'acc_map', 'depth_map']
    ret_list = [all_ret[k] for k in k_extract]
    ret_dict = {k : all_ret[k] for k in all_ret if k not in k_extract}
    return ret_list + [ret_dict]


def render_path(render_poses, hwf, chunk, render_kwargs, gt_imgs=None, savedir=None, render_factor=0):

    H, W, focal = hwf

    if render_factor!=0:
        # Render downsampled for speed
        H = H//render_factor
        W = W//render_factor
        focal = focal/render_factor

    rgbs = []
    disps = []
    psnrs = []
    accs = []
    t = time.time()
    for i, c2w in enumerate(tqdm(render_poses)):
        print(i, time.time() - t)
        t = time.time()
        rgb, disp, acc, depth, extras = render(H, W, focal, chunk=chunk, c2w=c2w[:3,:4], retraw=True, **render_kwargs)
        rgbs.append(rgb.cpu().numpy())
        disps.append(disp.cpu().numpy())
        accs.append(acc.cpu().numpy())
        if i==0:
            print(rgb.shape, disp.shape)
        
        """
        if gt_imgs is not None and render_factor==0:
            p = -10. * np.log10(np.mean(np.square(rgb.cpu().numpy() - gt_imgs[i])))
            print(p)
        """
        if savedir is not None:
            rgb8 = to8b(rgb.cpu().numpy())
            rgb8[np.isnan(rgb8)] = 0
            filename = os.path.join(savedir, '{:03d}.png'.format(i))
            imageio.imwrite(filename, rgb8)
            depth = depth.cpu().numpy()
            print("max:", np.nanmax(depth))
            depth = depth / 5 * 255
            imageio.imwrite(os.path.join(savedir, '{:03d}_depth.png'.format(i)), depth.astype(np.uint8))
        
        del rgb 
        del disp
        del acc
        del extras
        del depth
        torch.cuda.empty_cache()

    rgbs = np.stack(rgbs, 0)
    disps = np.stack(disps, 0)

    return rgbs, disps

def render_test_ray(rays_o, rays_d, hwf, ndc, near, far, use_viewdirs, N_samples, network, network_query_fn, **kwargs):
    H, W, focal = hwf
    if use_viewdirs:
        # provide ray directions as input
        viewdirs = rays_d
        viewdirs = viewdirs / torch.norm(viewdirs, dim=-1, keepdim=True)
        viewdirs = torch.reshape(viewdirs, [-1,3]).float()

    if ndc:
        # for forward facing scenes
        rays_o, rays_d = ndc_rays(H, W, focal, 1., rays_o, rays_d)

    # Create ray batch
    rays_o = torch.reshape(rays_o, [-1,3]).float()
    rays_d = torch.reshape(rays_d, [-1,3]).float()

    near, far = near * torch.ones_like(rays_d[...,:1]), far * torch.ones_like(rays_d[...,:1])

    t_vals = torch.linspace(0., 1., steps=N_samples).to(device)
    z_vals = near * (1.-t_vals) + far * (t_vals)

    z_vals = z_vals.reshape([rays_o.shape[0], N_samples])

    rgb, sigma, depth_maps, weights = sample_sigma(rays_o, rays_d, viewdirs, network, z_vals, network_query_fn)

    return rgb, sigma, z_vals, depth_maps, weights


def create_nerf(args):
    """Instantiate NeRF's MLP model.
    """
    embed_fn, input_ch = get_embedder(args.multires, args.i_embed)

    input_ch_views = 0
    embeddirs_fn = None
    if args.use_viewdirs:
        embeddirs_fn, input_ch_views = get_embedder(args.multires_views, args.i_embed)
    output_ch = 5 if args.N_importance > 0 else 4
    skips = [4]
    if args.alpha_model_path is None:
        model = NeRF(D=args.netdepth, W=args.netwidth,
                    input_ch=input_ch, output_ch=output_ch, skips=skips,
                    input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs).to(device)
        grad_vars = list(model.parameters())
    else:
        alpha_model = NeRF(D=args.netdepth_fine, W=args.netwidth_fine,
                            input_ch=input_ch, output_ch=output_ch, skips=skips,
                            input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs).to(device)
        print('Alpha model reloading from', args.alpha_model_path)
        ckpt = torch.load(args.alpha_model_path)
        alpha_model.load_state_dict(ckpt['network_fine_state_dict'])
        if not args.no_coarse:
            model = NeRF_RGB(D=args.netdepth, W=args.netwidth,
                        input_ch=input_ch, output_ch=output_ch, skips=skips,
                        input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs, alpha_model=alpha_model).to(device)
            grad_vars = list(model.parameters())
        else:
            model = None
            grad_vars = []
    

    model_fine = None
    if args.N_importance > 0:
        if args.alpha_model_path is None:
            model_fine = NeRF(D=args.netdepth_fine, W=args.netwidth_fine,
                            input_ch=input_ch, output_ch=output_ch, skips=skips,
                            input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs).to(device)
        else:
            model_fine = NeRF_RGB(D=args.netdepth_fine, W=args.netwidth_fine,
                            input_ch=input_ch, output_ch=output_ch, skips=skips,
                            input_ch_views=input_ch_views, use_viewdirs=args.use_viewdirs, alpha_model=alpha_model).to(device)
        grad_vars += list(model_fine.parameters())

    network_query_fn = lambda inputs, viewdirs, network_fn : run_network(inputs, viewdirs, network_fn,
                                                                embed_fn=embed_fn,
                                                                embeddirs_fn=embeddirs_fn,
                                                                netchunk=args.netchunk)

    # Create optimizer
    optimizer = torch.optim.Adam(params=grad_vars, lr=args.lrate, betas=(0.9, 0.999))

    start = 0
    basedir = args.basedir
    expname = args.expname

    ##########################

    # Load checkpoints
    if args.ft_path is not None and args.ft_path!='None':
        ckpts = [args.ft_path]
    else:
        ckpts = [os.path.join(basedir, expname, f) for f in sorted(os.listdir(os.path.join(basedir, expname))) if 'tar' in f]

    print('Found ckpts', ckpts)
    if len(ckpts) > 0 and not args.no_reload:
        ckpt_path = ckpts[-1]
        if args.ckpt_render_iter is not None:
            ckpt_path = os.path.join(os.path.join(basedir,expname, f'{args.ckpt_render_iter:06d}.tar'))

        print('Reloading from', ckpt_path)
        ckpt = torch.load(ckpt_path)

        start = ckpt['global_step']
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])

        # Load model
        model.load_state_dict(ckpt['network_fn_state_dict'])
        if model_fine is not None:
            model_fine.load_state_dict(ckpt['network_fine_state_dict'])

    ##########################

    render_kwargs_train = {
        'network_query_fn' : network_query_fn,
        'perturb' : args.perturb,
        'N_importance' : args.N_importance,
        'network_fine' : model_fine,
        'N_samples' : args.N_samples,
        'network_fn' : model,
        'use_viewdirs' : args.use_viewdirs,
        'white_bkgd' : args.white_bkgd,
        'raw_noise_std' : args.raw_noise_std,
        'entropy_ray_zvals' : args.entropy,
        'extract_alpha': args.smoothing
    }

    # NDC only good for LLFF-style forward facing data
    if args.dataset_type != 'llff' or args.no_ndc:
        print('Not ndc!')
        render_kwargs_train['ndc'] = False
        render_kwargs_train['lindisp'] = args.lindisp
    else:
        render_kwargs_train['ndc'] = True

    render_kwargs_test = {k : render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test['perturb'] = False
    render_kwargs_test['raw_noise_std'] = 0.


    ##########################

    return render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer


def render_rays(ray_batch,
                network_fn,
                network_query_fn,
                N_samples,
                retraw=False,
                lindisp=False,
                perturb=0.,
                N_importance=0,
                network_fine=None,
                white_bkgd=False,
                raw_noise_std=0.,
                verbose=False,
                pytest=False,
                sigma_loss=None,
                entropy_ray_zvals=None,
                extract_xyz=None,
                extract_alpha=None,
                extract_sigma=None,
                ):
    """Volumetric rendering.
    Args:
      ray_batch: array of shape [batch_size, ...]. All information necessary
        for sampling along a ray, including: ray origin, ray direction, min
        dist, max dist, and unit-magnitude viewing direction.
      network_fn: function. Model for predicting RGB and density at each point
        in space.
      network_query_fn: function used for passing queries to network_fn.
      N_samples: int. Number of different times to sample along each ray.
      retraw: bool. If True, include model's raw, unprocessed predictions.
      lindisp: bool. If True, sample linearly in inverse depth rather than in depth.
      perturb: float, 0 or 1. If non-zero, each ray is sampled at stratified
        random points in time.
      N_importance: int. Number of additional times to sample along each ray.
        These samples are only passed to network_fine.
      network_fine: "fine" network with same spec as network_fn.
      white_bkgd: bool. If True, assume a white background.
      raw_noise_std: ...
      verbose: bool. If True, print more debugging info.
    Returns:
      rgb_map: [num_rays, 3]. Estimated RGB color of a ray. Comes from fine model.
      disp_map: [num_rays]. Disparity map. 1 / depth.
      acc_map: [num_rays]. Accumulated opacity along each ray. Comes from fine model.
      raw: [num_rays, num_samples, 4]. Raw predictions from model.
      rgb0: See rgb_map. Output for coarse model.
      disp0: See disp_map. Output for coarse model.
      acc0: See acc_map. Output for coarse model.
      z_std: [num_rays]. Standard deviation of distances along ray for each
        sample.
    """
    N_rays = ray_batch.shape[0]
    rays_o, rays_d = ray_batch[:,0:3], ray_batch[:,3:6] # [N_rays, 3] each
    viewdirs = ray_batch[:,-3:] if ray_batch.shape[-1] > 9 else None
    bounds = torch.reshape(ray_batch[...,6:8], [-1,1,2])
    near, far = bounds[...,0], bounds[...,1] # [-1,1]

    t_vals = torch.linspace(0., 1., steps=N_samples).to(device)
    if not lindisp:
        z_vals = near * (1.-t_vals) + far * (t_vals)
    else:
        z_vals = 1./(1./near * (1.-t_vals) + 1./far * (t_vals))

    z_vals = z_vals.expand([N_rays, N_samples])

    if perturb > 0.:
        # get intervals between samples
        mids = .5 * (z_vals[...,1:] + z_vals[...,:-1])
        upper = torch.cat([mids, z_vals[...,-1:]], -1)
        lower = torch.cat([z_vals[...,:1], mids], -1)
        # stratified samples in those intervals
        t_rand = torch.rand(z_vals.shape).to(device)

        # Pytest, overwrite u with numpy's fixed random numbers
        if pytest:
            np.random.seed(0)
            t_rand = np.random.rand(*list(z_vals.shape))
            t_rand = torch.Tensor(t_rand).to(device)

        z_vals = lower + (upper - lower) * t_rand

    pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples, 3]


    if network_fn is not None:
        raw = network_query_fn(pts, viewdirs, network_fn)
        rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest)
    else:
        if network_fine.alpha_model is not None:
            raw = network_query_fn(pts, viewdirs, network_fine.alpha_model)
            rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest)
        else:
            raw = network_query_fn(pts, viewdirs, network_fine)
            rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest)


    if N_importance > 0:

        rgb_map_0, disp_map_0, acc_map_0 = rgb_map, disp_map, acc_map

        z_vals_mid = .5 * (z_vals[...,1:] + z_vals[...,:-1])
        z_samples = sample_pdf(z_vals_mid, weights[...,1:-1], N_importance, det=(perturb==0.), pytest=pytest)
        z_samples = z_samples.detach()

        z_vals, _ = torch.sort(torch.cat([z_vals, z_samples], -1), -1)
        pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples + N_importance, 3]

        run_fn = network_fn if network_fine is None else network_fine
        raw = network_query_fn(pts, viewdirs, run_fn)

        if entropy_ray_zvals or extract_sigma or extract_alpha:
            rgb_map, disp_map, acc_map, weights, depth_map, others = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest, out_sigma=True,out_alpha=True, out_dist=True)
        else:
             rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest)

    ret = {'rgb_map' : rgb_map, 'disp_map' : disp_map, 'acc_map' : acc_map, 'depth_map' : depth_map}
    
    if entropy_ray_zvals or extract_sigma or extract_alpha:
        ret['sigma'] = others['sigma']
        ret['alpha'] = others['alpha']
        ret['z_vals'] = z_vals
        ret['dists'] = others['dists']
    
    if extract_xyz:
        ret['xyz'] = torch.sum(weights.unsqueeze(-1)*pts, -2)
    if retraw:
        ret['raw'] = raw
    if N_importance > 0:
        ret['rgb0'] = rgb_map_0
        ret['disp0'] = disp_map_0
        ret['acc0'] = acc_map_0
        ret['z_std'] = torch.std(z_samples, dim=-1, unbiased=False)  # [N_rays]

    if sigma_loss is not None and ray_batch.shape[-1] > 11:
        depths = ray_batch[:,8]
        ret['sigma_loss'] = sigma_loss.calculate_loss(rays_o, rays_d, viewdirs, near, far, depths, network_query_fn, network_fine)

    for k in ret:
        if (torch.isnan(ret[k]).any() or torch.isinf(ret[k]).any()) and DEBUG:
            print('! [Numerical Error] {} contains nan or inf.'.format(k))
    return ret


def config_parser():

    import configargparse
    parser = configargparse.ArgumentParser()
    parser.add_argument('--config', is_config_file=True, 
                        help='config file path')
    parser.add_argument("--expname", type=str, 
                        help='experiment name')
    parser.add_argument("--basedir", type=str, default='./logs/', 
                        help='where to store ckpts and logs')
    parser.add_argument("--datadir", type=str, default='./data/llff/fern', 
                        help='input data directory')
    parser.add_argument("--maskdir", type=str, 
                        help='mask data directory')
    parser.add_argument("--fewshot_seed", type=int, default=0, 
                        help='fewshot_seed')
    # training options
    parser.add_argument("--netdepth", type=int, default=8, 
                        help='layers in network')
    parser.add_argument("--netwidth", type=int, default=256, 
                        help='channels per layer')
    parser.add_argument("--netdepth_fine", type=int, default=8, 
                        help='layers in fine network')
    parser.add_argument("--netwidth_fine", type=int, default=256, 
                        help='channels per layer in fine network')
    parser.add_argument("--N_rand", type=int, default=32*32*4, 
                        help='batch size (number of random rays per gradient step)')
    parser.add_argument("--lrate", type=float, default=5e-4, 
                        help='learning rate')
    parser.add_argument("--lrate_decay", type=int, default=250, 
                        help='exponential learning rate decay (in 1000 steps)')
    parser.add_argument("--chunk", type=int, default=1024*32, 
                        help='number of rays processed in parallel, decrease if running out of memory')
    parser.add_argument("--netchunk", type=int, default=1024*64, 
                        help='number of pts sent through network in parallel, decrease if running out of memory')
    parser.add_argument("--no_batching", action='store_true', 
                        help='only take random rays from 1 image at a time')
    parser.add_argument("--no_reload", action='store_true', 
                        help='do not reload weights from saved ckpt')
    parser.add_argument("--ft_path", type=str, default=None, 
                        help='specific weights npy file to reload for coarse network')

    #######################################################
    #         Ray Entropy Minimization Loss               #
    #######################################################
    
    #entropy
    parser.add_argument("--N_entropy", type=int, default=100,
                        help='number of entropy ray')
    
    # entropy type
    parser.add_argument("--entropy", action='store_true',
                        help='using entropy ray loss')
    parser.add_argument("--entropy_log_scaling", action='store_true',
                        help='using log scaling for entropy loss')
    parser.add_argument("--entropy_ignore_smoothing", action='store_true',
                        help='ignoring entropy for ray for smoothing')
    parser.add_argument("--entropy_end_iter", type=int, default=None,
                        help='end iteratio of entropy')
    parser.add_argument("--entropy_type", type=str, default='log2', choices=['log2', '1-p'],
                        help='choosing type of entropy')
    parser.add_argument("--entropy_acc_threshold", type=float, default=0.1,
                        help='threshold for acc masking')
    parser.add_argument("--computing_entropy_all", action='store_true',
                        help='computing entropy for both seen and unseen ')
    
    #lambda
    parser.add_argument("--entropy_ray_lambda", type=float, default=1,
                        help='entropy lambda for ray entropy loss')
    parser.add_argument("--entropy_ray_zvals_lambda", type=float, default=1,
                        help='entropy lambda for ray zvals entropy loss')
   

    #######################################################
    #         Infomation Gain Reduction Loss              #
    #######################################################

    parser.add_argument("--smoothing", action='store_true',
                        help='using information gain reduction loss')
    # choosing between rotating camera pose & near pixel
    parser.add_argument("--smooth_sampling_method", type=str, default='near_pose', 
        help='how to sample the near rays, near_pose: modifying camera pose, near_pixel: sample near pixel', 
                    choices=['near_pose', 'near_pixel'])
    # 1) sampling by rotating camera pose
    parser.add_argument("--near_c2w_type", type=str, default='rot_from_origin', 
                        help='random augmentation method')
    parser.add_argument("--near_c2w_rot", type=float, default=5, 
                        help='random augmentation rotate: degree')
    parser.add_argument("--near_c2w_trans", type=float, default=0.1, 
                        help='random augmentation translation')
    # 2) sampling with near pixel
    parser.add_argument("--smooth_pixel_range", type=int,
                        help='the maximum distance between the near ray & the original ray (pixel dimension)')
    # optimizing 
    parser.add_argument("--smoothing_lambda", type=float, default=1, 
                        help='lambda for smoothing loss')
    parser.add_argument("--smoothing_activation", type=str, default='norm', 
                        help='how to make alpha to the distribution')
    parser.add_argument("--smoothing_step_size", type=int, default='5000',
                        help='reducing smoothing every')
    parser.add_argument("--smoothing_rate", type=float, default=1,
                        help='reducing smoothing rate')
    parser.add_argument("--smoothing_end_iter", type=int, default=None,
                        help='when smoothing will be end')
    
    #######################################################
    #                      Others                         #
    #######################################################

    # rendering options
    parser.add_argument("--N_samples", type=int, default=64, 
                        help='number of coarse samples per ray')
    parser.add_argument("--N_importance", type=int, default=0,
                        help='number of additional fine samples per ray')
    parser.add_argument("--perturb", type=float, default=1.,
                        help='set to 0. for no jitter, 1. for jitter')
    parser.add_argument("--use_viewdirs", action='store_true', 
                        help='use full 5D input instead of 3D')
    parser.add_argument("--i_embed", type=int, default=0, 
                        help='set 0 for default positional encoding, -1 for none')
    parser.add_argument("--multires", type=int, default=10, 
                        help='log2 of max freq for positional encoding (3D location)')
    parser.add_argument("--multires_views", type=int, default=4, 
                        help='log2 of max freq for positional encoding (2D direction)')
    parser.add_argument("--raw_noise_std", type=float, default=0., 
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')

    # rendering options
    parser.add_argument("--render_only", action='store_true', 
                        help='do not optimize, reload weights and render out render_poses path')
    parser.add_argument("--eval_only", action='store_true', 
                        help='do not optimize, reload weights and evaluation and logging to wandb')
    parser.add_argument("--render_test", action='store_true', 
                        help='render the test set instead of render_poses path')
    parser.add_argument("--render_test_full", action='store_true', 
                        help='render the test set instead of render_poses path')
    parser.add_argument("--ckpt_render_iter", type=int, default=None, 
                        help='checkpoint iteration')
    
    parser.add_argument("--render_test_ray", action='store_true', 
                        help='render the test set instead of render_poses path')
    parser.add_argument("--render_train", action='store_true', 
                        help='render the train set instead of render_poses path')  
    parser.add_argument("--render_mypath", action='store_true', 
                        help='render the test path')         
    parser.add_argument("--render_factor", type=int, default=0, 
                        help='downsampling factor to speed up rendering, set 4 or 8 for fast preview')
    parser.add_argument("--render_pass", action='store_true', 
                        help='do not rendering when resume')
    
    # training options
    parser.add_argument("--precrop_iters", type=int, default=0,
                        help='number of steps to train on central crops')
    parser.add_argument("--precrop_frac", type=float,
                        default=.5, help='fraction of img taken for central crops') 

    # dataset options
    parser.add_argument("--dataset_type", type=str, default='llff', 
                        help='options: llff / blender / deepvoxels')
    parser.add_argument("--testskip", type=int, default=8, 
                        help='will load 1/N images from test/val sets, useful for large datasets like deepvoxels')
    parser.add_argument("--fewshot", type=int, default=0, 
            help='if 0 not using fewshot, else: using fewshot')

    ## deepvoxels flags
    parser.add_argument("--shape", type=str, default='greek', 
                        help='options : armchair / cube / greek / vase')

    ## blender flags
    parser.add_argument("--white_bkgd", action='store_true', 
                        help='set to render synthetic data on a white bkgd (always use for dvoxels)')
    parser.add_argument("--half_res", action='store_true', 
                        help='load blender synthetic data at 400x400 instead of 800x800')

    ## llff flags
    parser.add_argument("--factor", type=int, default=8, 
                        help='downsample factor for LLFF images')
    parser.add_argument("--no_ndc", action='store_true', 
                        help='do not use normalized device coordinates (set for non-forward facing scenes)')
    parser.add_argument("--lindisp", action='store_true', 
                        help='sampling linearly in disparity rather than depth')
    parser.add_argument("--spherify", action='store_true', 
                        help='set for spherical 360 scenes')
    parser.add_argument("--llffhold", type=int, default=8, 
                        help='will take every 1/N images as LLFF test set, paper uses 8')

    # logging/saving options
    parser.add_argument("--i_print",   type=int, default=100, 
                        help='frequency of console printout and metric loggin')
    parser.add_argument("--i_img",     type=int, default=500, 
                        help='frequency of tensorboard image logging')
    parser.add_argument("--i_weights", type=int, default=10000, 
                        help='frequency of weight ckpt saving')
    parser.add_argument("--i_testset", type=int, default=50000, 
                        help='frequency of testset saving')
    parser.add_argument("--i_video",   type=int, default=50000, 
                        help='frequency of render_poses video saving')
    parser.add_argument("--wandb",  action='store_true')
    parser.add_argument("--i_wandb",   type=int, default=100, 
                        help='frequency of logging on wandb(iteration)')
    parser.add_argument("--wandb_group",   type=str, 
                        help='wandb group name')
    # debug
    parser.add_argument("--debug",  action='store_true')

    # new experiment by kangle
    parser.add_argument("--N_iters", type=int, default=200000, 
                        help='number of iters')
    parser.add_argument("--alpha_model_path", type=str, default=None,
                        help='predefined alpha model')
    parser.add_argument("--no_coarse", action='store_true',
                        help="Remove coarse network.")
    parser.add_argument("--train_scene", nargs='+', type=int,
                        help='id of scenes used to train')
    parser.add_argument("--test_scene", nargs='+', type=int,
                        help='id of scenes used to test')
    
    return parser


def train():

    parser = config_parser()
    args = parser.parse_args()

    render_first_time=True
    if args.render_pass:
        render_first_time=False

    ########################################
    #              DTU                     #
    ########################################
    if args.dataset_type == 'llff':
        data_info = torch.load('./data/nerf_llff_data/data_splits.pth')
        if args.datadir.split('/')[-1] in data_info.keys():
            category = args.datadir.split('/')[-1]
        if args.datadir.split('/')[-2] in data_info.keys():
            category = args.datadir.split('/')[-2]
        full_datadir = os.path.join('data/nerf_llff_data/', category)
        images, poses, bds, render_poses, i_test = load_llff_data(full_datadir, args.factor,
                                                                  recenter=True, bd_factor=.75,
                                                                  spherify=args.spherify)
        hwf = poses[0,:3,-1]
        poses = poses[:,:3,:4]
        print('Loaded llff', images.shape, render_poses.shape, hwf, args.datadir)
        
        if not isinstance(i_test, list):
            i_test = [i_test]

        if args.llffhold > 0:
            print('Auto LLFF holdout,', args.llffhold)
            i_test = np.arange(images.shape[0])[::args.llffhold]

        if args.test_scene is not None:
            i_test = np.array([i for i in args.test_scene])

        if i_test[0] < 0:
            # i_test = []
            i_test = np.array([i for i in range(len(poses)) if (i not in i_train)])

        i_val = i_test
        
        if args.train_scene is None:
            i_train = np.array([i for i in np.arange(int(images.shape[0])) if
                        (i not in i_test and i not in i_val)])
        else:
            i_train = np.array([i for i in args.train_scene if
                        (i not in i_test and i not in i_val)])
       
        if args.fewshot > 0:
            i_train = data_info[category][f'{args.fewshot}shot_split'][0]
            if args.train_scene is None:
                np.random.seed(args.fewshot_seed)
                i_train = np.random.choice(i_train, args.fewshot, replace=False)
            else:
                i_train = np.array(args.train_scene)
        print('i_train', i_train)
        print('DEFINING BOUNDS')
        
        if args.no_ndc:
            near = np.ndarray.min(bds) * .9
            far = np.ndarray.max(bds) * 1.
        else:
            near = 0.
            far = 1.
        print('NEAR FAR', near, far)

    ########################################
    #              DTU                     #
    ########################################

    elif args.dataset_type == 'dtu':
        images, poses, hwf, masks = load_dtu_data(args.datadir, args.train_scene, args.maskdir)
        render_poses = poses
        print('Loaded DTU', images.shape, poses.shape, hwf, args.datadir)
        if args.test_scene is not None:
            i_test = np.array([i for i in args.test_scene])

        if i_test[0] < 0:
            i_test = []

        i_val = i_test
        if args.train_scene is None:
            i_train = np.array([i for i in np.arange(int(images.shape[0])) if
                        (i not in i_test and i not in i_val)])
        else:
            i_train = np.array([i for i in args.train_scene if
                        (i not in i_test and i not in i_val)])
        
        i_test = np.array([i for i in range(len(poses)) if (i not in i_train)])
        
        near = 0.1
        far = 5.0
    
    ########################################
    #              Blender                 #
    ########################################
    
    elif args.dataset_type == 'blender':
        images, poses, render_poses, hwf, i_split = load_blender_data(args.datadir, args.half_res, args.testskip)
        print('Loaded blender', images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split
        near = 2.
        far = 6.

        if args.fewshot > 0:
            if args.train_scene is None:
                np.random.seed(args.fewshot_seed)
                i_train = np.random.choice(i_train, args.fewshot, replace=False)
            else:
                i_train = np.array(args.train_scene)
            print('i_train', i_train)
        
        images_mask = images[...,-1]
        if args.white_bkgd:
            images = images[...,:3]*images[...,-1:] + (1.-images[...,-1:])
        else:
            images = images[...,:3]

    ########################################
    #          OpenIllumination            #
    ########################################

    elif args.dataset_type == 'oppo':
        images, poses, render_poses, hwf, i_split = load_oppo_data(args.datadir, args.half_res, args.testskip, factor=args.factor)
        print('Loaded oppo', images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split
        near = 0.5
        far = 1.5

        if args.fewshot > 0:
            if args.train_scene is None:
                np.random.seed(args.fewshot_seed)
                i_train = np.random.choice(i_train, args.fewshot, replace=False)
            else:
                i_train = np.array(args.train_scene)
            print('i_train', i_train)
        
        images_mask = images[...,-1]
        if args.white_bkgd:
            images = images[...,:3]*images[...,-1:] + (1.-images[...,-1:])
        else:
            images = images[...,:3]
    
    else:
        print('Unknown dataset type', args.dataset_type, 'exiting')
        return

    # Cast intrinsics to right types
    H, W, focal = hwf
    H, W = int(H), int(W)
    hwf = [H, W, focal]

    if args.render_test:
        render_poses = np.array(poses[i_test])
    elif args.render_train:
        render_poses = np.array(poses[i_train])
    elif args.render_mypath:
        # render_poses = generate_renderpath(np.array(poses[i_test]), focal)
        render_poses = generate_renderpath(np.array(poses[i_test])[3:4], focal, sc=1)

    # Create log dir and copy the config file
    basedir = args.basedir
    expname = args.expname
    os.makedirs(os.path.join(basedir, expname), exist_ok=True)
    f = os.path.join(basedir, expname, 'args.txt')
    with open(f, 'w') as file:
        for arg in sorted(vars(args)):
            attr = getattr(args, arg)
            file.write('{} = {}\n'.format(arg, attr))
    if args.config is not None:
        f = os.path.join(basedir, expname, 'config.txt')
        with open(f, 'w') as file:
            file.write(open(args.config, 'r').read())

    # Create nerf model
    render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer = create_nerf(args)


    global_step = start

    bds_dict = {
        'near' : near,
        'far' : far,
    }
    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)

    # Move testing data to GPU
    render_poses = torch.Tensor(render_poses).to(device)

    # Short circuit if only rendering out from trained model
    if args.render_only:
        print('RENDER ONLY')
        with torch.no_grad():
            if args.render_test:
                # render_test switches to test poses
                images = images[i_test]
            else:
                # Default is smoother render_poses path
                images = None

            if args.render_test:
                if args.render_test_full:
                    testsavedir = os.path.join(basedir, expname, 'full_renderonly_{}_{:06d}'.format('test', start))
                else:
                    testsavedir = os.path.join(basedir, expname, 'renderonly_{}_{:06d}'.format('test', start))
            elif args.render_train:
                testsavedir = os.path.join(basedir, expname, 'renderonly_{}_{:06d}'.format('train', start))
            else:
                testsavedir = os.path.join(basedir, expname, 'renderonly_{}_{:06d}'.format('path', start))
            os.makedirs(testsavedir, exist_ok=True)
            print('test poses shape', render_poses.shape)

            rgbs, disps = render_path(render_poses, hwf, args.chunk, render_kwargs_test, gt_imgs=images, savedir=testsavedir, render_factor=args.render_factor)
            print('Done rendering', testsavedir)
            imageio.mimwrite(os.path.join(testsavedir, 'rgb.mp4'), to8b(rgbs), fps=30, quality=8)
            disps[np.isnan(disps)] = 0
            print('Depth stats', np.mean(disps), np.max(disps), np.percentile(disps, 95))
            imageio.mimwrite(os.path.join(testsavedir, 'disp.mp4'), to8b(disps / np.percentile(disps, 95)), fps=30, quality=8)
            return

    # Prepare raybatch tensor if batching random rays
    N_rgb = args.N_rand
    
    if args.entropy:
        N_entropy = args.N_entropy
        fun_entropy_loss = EntropyLoss(args)

    if args.smoothing:
        get_near_c2w = GetNearC2W(args)
        fun_KL_divergence_loss = SmoothingLoss(args)

    use_batching = not args.no_batching
   
    if use_batching:
        # For random ray batching
        print('get rays')
        rays = np.stack([get_rays_np(H, W, focal, p) for p in poses[:,:3,:4]], 0) # [N, ro+rd, H, W, 3]
        if args.debug:
            print('rays.shape:', rays.shape)
        print('done, concats')
        rays_rgb = np.concatenate([rays, images[:,None]], 1) # [N, ro+rd+rgb, H, W, 3]
        if args.debug:
            print('rays_rgb.shape:', rays_rgb.shape)
        rays_all = np.transpose(rays_rgb, [0,2,3,1,4]) # [N, H, W, ro+rd+rgb, 3]
        rays_rgb = np.stack([rays_all[i] for i in i_train], 0) # train images only
        rays_rgb = np.reshape(rays_rgb, [-1,3,3]) # [(N-1)*H*W, ro+rd+rgb, 3]
        rays_rgb = rays_rgb.astype(np.float32)
        print('shuffle rays')
        np.random.shuffle(rays_rgb)

        rays_depth = None
        rays_entropy = None
               
        if args.entropy:
            rays_entropy = np.stack(rays_all, 0) # train images only
            rays_entropy = np.reshape(rays_entropy, [-1,3,3]) # [(N-1)*H*W, ro+rd+rgb, 3]
            rays_entropy = rays_entropy.astype(np.float32)
            np.random.shuffle(rays_entropy)

    if args.debug:
        return
    # Move training data to GPU
    images = torch.Tensor(images).to(device)
    poses = torch.Tensor(poses).to(device)
    
    if use_batching:
        # rays_rgb = torch.Tensor(rays_rgb).to(device)
        # rays_depth = torch.Tensor(rays_depth).to(device) if rays_depth is not None else None
        raysRGB_iter = iter(DataLoader(RayDataset(rays_rgb), batch_size = N_rgb, shuffle=True, num_workers=0))
        raysDepth_iter = iter(DataLoader(RayDataset(rays_depth), batch_size = N_depth, shuffle=True, num_workers=0)) if rays_depth is not None else None
        raysEntropy_iter = iter(DataLoader(RayDataset(rays_entropy), batch_size = N_entropy, shuffle=True, num_workers=0)) if rays_entropy is not None else None

    N_iters = args.N_iters + 1
    print('Begin')
    print('TRAIN views are', i_train)
    print('TEST views are', i_test)
    print('VAL views are', i_val)

    # Summary writers
    #writer = SummaryWriter(os.path.join(basedir, 'summaries', expname))
    tags = []
    if (not args.debug) and args.wandb:
        wandb.init(project='entropy_nerf', group=args.wandb_group,config=args, name=args.expname, tags=tags)
   
    start = start + 1

    if args.eval_only:
        N_iters = start + 2
        i_testset = 1
    for i in trange(start, N_iters):
        time0 = time.time()

        # Sample random ray batch
        if use_batching:
            # Random over all images
            try:
                batch = next(raysRGB_iter).to(device)
            except StopIteration:
                raysRGB_iter = iter(DataLoader(RayDataset(rays_rgb), batch_size = N_rgb, shuffle=True, num_workers=0))
                batch = next(raysRGB_iter).to(device)
            batch = torch.transpose(batch, 0, 1)
            batch_rays, target_s = batch[:2], batch[2]

            if args.entropy and (args.N_entropy!=0):
                try:
                    batch_entropy = next(raysEntropy_iter).to(device)
                except StopIteration:
                    raysEntropy_iter = iter(DataLoader(RayDataset(rays_entropy), batch_size = N_entropy, shuffle=True, num_workers=0))
                    batch_entropy = next(raysEntropy_iter).to(device)
                batch_rays_entropy = torch.transpose(batch_entropy, 0, 1)[:2]
                
        else:
            # Random from one image
            img_i = np.random.choice(i_train)
            target = images[img_i]
                
            rgb_pose = poses[img_i, :3,:4]
            
            if args.N_rand is not None:
                rays_o, rays_d = get_rays(H, W, focal, torch.Tensor(rgb_pose))  # (H, W, 3), (H, W, 3)

                if i < args.precrop_iters:
                    dH = int(H//2 * args.precrop_frac)
                    dW = int(W//2 * args.precrop_frac)
                    coords = torch.stack(
                        torch.meshgrid(
                            torch.linspace(H//2 - dH, H//2 + dH - 1, 2*dH), 
                            torch.linspace(W//2 - dW, W//2 + dW - 1, 2*dW)
                        ), -1)
                    if i == start:
                        print(f"[Config] Center cropping of size {2*dH} x {2*dW} is enabled until iter {args.precrop_iters}")                
                else:
                    coords = torch.stack(torch.meshgrid(torch.linspace(0, H-1, H), torch.linspace(0, W-1, W)), -1)  # (H, W, 2)

                coords = torch.reshape(coords, [-1,2])  # (H * W, 2)
                select_inds = np.random.choice(coords.shape[0], size=[N_rgb], replace=False)  # (N_rand,)
                select_coords = coords[select_inds].long()  # (N_rand, 2)
                rays_o = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                rays_d = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                batch_rays = torch.stack([rays_o, rays_d], 0) # (2, N_rand, 3)
                target_s = target[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                
                if args.smoothing:
                    rgb_near_pose = get_near_c2w(rgb_pose, iter_=i)
                    near_rays_o, near_rays_d = get_rays(H, W, focal, torch.Tensor(rgb_near_pose))  # (H, W, 3), (H, W, 3)
                    near_rays_o = near_rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                    near_rays_d = near_rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                    near_batch_rays = torch.stack([near_rays_o, near_rays_d], 0) # (2, N_rand, 3)
            
            ########################################################
            #            Sampling for unseen rays                  #
            ########################################################
            
            if args.entropy and (args.N_entropy !=0):
                img_i = np.random.choice(len(images))
                target = images[img_i]
                pose = poses[img_i, :3,:4]
                if args.smooth_sampling_method == 'near_pixel':
                    if args.smooth_pixel_range is None:
                        raise Exception('The near pixel is not defined')
                    rays_o, rays_d = get_rays(H, W, focal, torch.Tensor(pose), padding=args.smooth_pixel_range)  # (H, W, 3), (H, W, 3)
                else:
                    rays_o, rays_d = get_rays(H, W, focal, torch.Tensor(pose))  # (H, W, 3), (H, W, 3)
                
                if i < args.precrop_iters:
                    dH = int(H//2 * args.precrop_frac)
                    dW = int(W//2 * args.precrop_frac)
                    coords = torch.stack(
                        torch.meshgrid(
                            torch.linspace(H//2 - dH, H//2 + dH - 1, 2*dH), 
                            torch.linspace(W//2 - dW, W//2 + dW - 1, 2*dW)
                        ), -1)
                    if i == start:
                        print(f"[Config] Center cropping of size {2*dH} x {2*dW} is enabled until iter {args.precrop_iters}")   
                else:
                    if args.smooth_sampling_method == 'near_pixel':
                        padding = args.smooth_pixel_range
                        coords = torch.stack(
                                torch.meshgrid(torch.linspace(padding, H-1+padding, H), 
                                torch.linspace(padding, W-1+padding, W)), -1)  # (H, W, 2)
                    else:
                        coords = torch.stack(torch.meshgrid(torch.linspace(0, H-1, H), torch.linspace(0, W-1, W)), -1)  # (H, W, 2)
                
                coords = torch.reshape(coords, [-1,2])  # (H * W, 2)
                select_inds = np.random.choice(coords.shape[0], size=[N_entropy], replace=False)  # (N_rand,)
                select_coords = coords[select_inds].long()  # (N_rand, 2)
                rays_o_ent = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                rays_d_ent = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                batch_rays_entropy = torch.stack([rays_o_ent, rays_d_ent], 0) # (2, N_rand, 3)
                
                ########################################################
                #   Ray sampling for information gain reduction loss   #
                ########################################################

                if args.smoothing:
                    if args.smooth_sampling_method == 'near_pixel':
                        near_select_coords = get_near_pixel(select_coords, args.smooth_pixel_range)
                        ent_near_rays_o = rays_o[near_select_coords[:, 0], near_select_coords[:, 1]]  # (N_rand, 3)
                        ent_near_rays_d = rays_d[near_select_coords[:, 0], near_select_coords[:, 1]]  # (N_rand, 3)
                        ent_near_batch_rays = torch.stack([ent_near_rays_o, ent_near_rays_d], 0) # (2, N_rand, 3)
                    elif args.smooth_sampling_method == 'near_pose':
                        ent_near_pose = get_near_c2w(pose,iter_=i)
                        ent_near_rays_o, ent_near_rays_d = get_rays(H, W, focal, torch.Tensor(ent_near_pose))  # (H, W, 3), (H, W, 3)
                        ent_near_rays_o = ent_near_rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                        ent_near_rays_d = ent_near_rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                        ent_near_batch_rays = torch.stack([ent_near_rays_o, ent_near_rays_d], 0) # (2, N_rand, 3)


        N_rgb = batch_rays.shape[1]

        if args.entropy and (args.N_entropy !=0):
            batch_rays = torch.cat([batch_rays, batch_rays_entropy], 1)
            
        if args.smoothing:
            if args.entropy and (args.N_entropy !=0):
                batch_rays = torch.cat([batch_rays, near_batch_rays, ent_near_batch_rays], 1)
            else: 
                batch_rays = torch.cat([batch_rays, near_batch_rays], 1)
        rgb, disp, acc, depth, extras = render(H, W, focal, chunk=args.chunk, rays=batch_rays,
                                                verbose=i < 10, retraw=True,
                                                **render_kwargs_train)
        
        if args.entropy:
            acc_raw = acc 
            alpha_raw = extras['alpha']
            dists_raw = extras['dists']

        extras = {x:extras[x][:N_rgb] for x in extras}

        rgb = rgb[:N_rgb, :]
        disp = disp[:N_rgb] 
        acc = acc[:N_rgb]
        
        optimizer.zero_grad()
        img_loss = img2mse(rgb, target_s)
        logging_info = {'rgb_loss': img_loss} 
        entropy_ray_zvals_loss = 0
        smoothing_loss = 0
       
        ########################################################
        #            Ray Entropy Minimiation Loss              #
        ########################################################

        if args.entropy:
            entropy_ray_zvals_loss = fun_entropy_loss.ray_zvals(alpha_raw, acc_raw)
            logging_info['entropy_ray_zvals'] = entropy_ray_zvals_loss
        
        if args.entropy_end_iter is not None:
            if i > args.entropy_end_iter:
                entropy_ray_zvals_loss = 0
        
        ########################################################
        #           Infomation Gain Reduction Loss             #
        ########################################################
        
        smoothing_lambda = args.smoothing_lambda * args.smoothing_rate ** (int(i/args.smoothing_step_size))
        
        if args.smoothing:
            smoothing_loss = fun_KL_divergence_loss(alpha_raw)
            logging_info['KL_loss'] = smoothing_loss
            if args.smoothing_end_iter is not None:
                if i > args.smoothing_end_iter:
                    smoothing_loss = 0
       

        trans = extras['raw'][...,-1]
        loss = img_loss \
                + args.entropy_ray_zvals_lambda * entropy_ray_zvals_loss \
                + smoothing_lambda * smoothing_loss
        psnr = mse2psnr(img_loss)
        logging_info['psnr'] = psnr

        if 'rgb0' in extras and not args.no_coarse:
            img_loss0 = img2mse(extras['rgb0'], target_s)
            loss = loss + img_loss0
            psnr0 = mse2psnr(img_loss0)
            logging_info['rgb0_loss'] = img_loss0
            logging_info['psnr0'] = psnr0
            
        if args.wandb:
            if i % args.i_wandb == 0:
                wandb.log(logging_info, step=i)
            
        loss.backward()
        optimizer.step()

        # NOTE: IMPORTANT!
        ###   update learning rate   ###
        decay_rate = 0.1
        decay_steps = args.lrate_decay * 1000
        new_lrate = args.lrate * (decay_rate ** (global_step / decay_steps))
        for param_group in optimizer.param_groups:
            param_group['lr'] = new_lrate

        dt = time.time()-time0

        # Rest is logging
        if i%args.i_weights==0:
            path = os.path.join(basedir, expname, '{:06d}.tar'.format(i))
            torch.save({
                'global_step': global_step,
                'network_fn_state_dict': render_kwargs_train['network_fn'].state_dict() if render_kwargs_train['network_fn'] is not None else None,
                'network_fine_state_dict': render_kwargs_train['network_fine'].state_dict() if render_kwargs_train['network_fine'] is not None else None,
                'optimizer_state_dict': optimizer.state_dict(),
            }, path)
            print('Saved checkpoints at', path)

        if (args.i_video > 0 and i%args.i_video==0 and i > 0):
            # Turn on testing mode
            if render_first_time==False:
                render_first_time=True
                continue                
            with torch.no_grad():
                rgbs, disps = render_path(render_poses, hwf, args.chunk, render_kwargs_test)
            print('Done, saving', rgbs.shape, disps.shape)
            moviebase = os.path.join(basedir, expname, '{}_spiral_{:06d}_'.format(expname.split('/')[-1], i))
            imageio.mimwrite(moviebase + 'rgb.mp4', to8b(rgbs), fps=30, quality=8)
            imageio.mimwrite(moviebase + 'disp.mp4', to8b(disps / np.nanmax(disps)), fps=30, quality=8)

        if (i%args.i_testset==0 ) and (i > 0) and (len(i_test) > 0):
            testsavedir = os.path.join(basedir, expname, 'testset_{:06d}'.format(i))
            os.makedirs(testsavedir, exist_ok=True)
            print('test poses shape', poses[i_test].shape)
            with torch.no_grad():
                rgbs, disps = render_path(torch.Tensor(poses[i_test]).to(device), hwf, args.chunk, render_kwargs_test, gt_imgs=images[i_test], savedir=testsavedir, render_factor=args.render_factor)
            print('Saved test set')

            filenames = [os.path.join(testsavedir, '{:03d}.png'.format(k)) for k in range(len(i_test))]

            test_loss = img2mse(torch.Tensor(rgbs), images[i_test])
            test_psnr = mse2psnr(test_loss)
            
            test_redefine_psnr = img2psnr_redefine(torch.Tensor(rgbs), images[i_test])
           
            test_ssim, test_msssim = img2ssim(torch.Tensor(rgbs), images[i_test])

            # test_lpips = img2lpips(torch.Tensor(rgbs), images[i_test], device=device)
            # print(f"TEST_LPIPS: {test_lpips}")
            
            if args.wandb:
                wandb.log({ 'test_psnr': test_psnr, 
                            'test_psnr_re' : test_redefine_psnr,
                            'test_ssim': test_ssim,
                            # 'test_lpips': test_lpips
                    }, step=i)
            
            if args.dataset_type == 'dtu':
                if args.maskdir is not None:
                    test_psnr_mask = img2psnr_mask(torch.Tensor(rgbs), images[i_test], torch.Tensor(masks[i_test]))
                    test_ssim_m, test_msssim_m = img2ssim(torch.Tensor(rgbs), images[i_test], torch.Tensor(masks[i_test]))
                
                    if args.wandb:
                        wandb.log({ 'test_psnr_mask': test_psnr_mask, 
                            'test_ssim_m': test_ssim_m, 
                            'test_msssim_m': test_msssim_m, 
                            }, step=i)

        if i%args.i_print==0:
            tqdm.write(f"[TRAIN] Iter: {i} Loss: {loss.item()}  PSNR: {psnr.item()}")
        global_step += 1


if __name__=='__main__':
    torch.set_default_tensor_type('torch.cuda.FloatTensor')
    train()
