import os
import torch
import numpy as np
import imageio
import json
import torch.nn.functional as F
import cv2


trans_t = lambda t: torch.Tensor(
    [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, t], [0, 0, 0, 1]]
).float()

rot_phi = lambda phi: torch.Tensor(
    [
        [1, 0, 0, 0],
        [0, np.cos(phi), -np.sin(phi), 0],
        [0, np.sin(phi), np.cos(phi), 0],
        [0, 0, 0, 1],
    ]
).float()

rot_theta = lambda th: torch.Tensor(
    [
        [np.cos(th), 0, -np.sin(th), 0],
        [0, 1, 0, 0],
        [np.sin(th), 0, np.cos(th), 0],
        [0, 0, 0, 1],
    ]
).float()


def pose_spherical(theta, phi, radius):
    c2w = trans_t(radius)
    c2w = rot_phi(phi / 180.0 * np.pi) @ c2w
    c2w = rot_theta(theta / 180.0 * np.pi) @ c2w
    c2w = (
        torch.Tensor(
            np.array([[-1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]])
        )
        @ c2w
    )
    return c2w


def load_oppo_data(basedir, half_res=False, testskip=1, num_render_poses=40, factor=0):
    splits = ["train", "test"]
    metas = {}
    for s in splits:
        with open(
            os.path.join(basedir, "../../transforms_alignz_{}.json".format(s)), "r"
        ) as fp:
            metas[s] = json.load(fp)["frames"]

    all_imgs = []
    all_poses = []
    counts = [0]
    for s in splits:
        meta = metas[s]
        imgs = []
        poses = []
        if s == "train" or testskip == 0:
            skip = 1
        else:
            skip = testskip

        for frame in list(meta.values())[::skip]:
            imgid = frame["file_path"].split("/")[-1]

            fname = os.path.join(basedir, f"../Lights/013/raw_undistorted/{imgid}.JPG")
            img = imageio.imread(fname)
            mask_path = os.path.join(basedir, f"com_masks/{imgid}.png")
            mask = cv2.imread(mask_path, 2) > 0
            # mask = torch.from_numpy(mask).bool()
            img = img * mask[..., None] + (1 - mask[..., None])
            img = np.concatenate([img, mask[..., None] * 255.0], axis=-1)

            imgs.append(img)
            poses.append(np.array(frame["transform_matrix"]))
        imgs = (np.array(imgs) / 255.0).astype(np.float32)  # keep all 4 channels (RGBA)
        poses = np.array(poses).astype(np.float32)
        counts.append(counts[-1] + imgs.shape[0])
        all_imgs.append(imgs)
        all_poses.append(poses)

    i_split = [np.arange(counts[i], counts[i + 1]) for i in [0, 1, 1]]

    imgs = np.concatenate(all_imgs, 0)
    poses = np.concatenate(all_poses, 0)

    H, W = imgs[0].shape[:2]
    first_meta = list(metas["train"].values())[0]
    focal = 0.5 * first_meta["calib_imgw"] / np.tan(0.5 * first_meta["camera_angle_x"])

    render_poses = torch.stack(
        [
            pose_spherical(angle, -30.0, 4.0)
            for angle in np.linspace(-180, 180, num_render_poses + 1)[:-1]
        ],
        0,
    )

    if half_res:
        H = H // 2
        W = W // 2
        focal = focal / 2

        imgs_downsampled = np.zeros((imgs.shape[0], H, W, 4))
        for i, img in enumerate(imgs):
            imgs_downsampled[i] = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        imgs = imgs_downsampled
    elif factor > 0:
        H = H // factor
        W = W // factor
        focal = focal / factor

        imgs_downsampled = np.zeros((imgs.shape[0], H, W, 4))
        for i, img in enumerate(imgs):
            imgs_downsampled[i] = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        imgs = imgs_downsampled

    return imgs, poses, render_poses, [H, W, focal], i_split
