import torch
import numpy as np
import modules.default_pipeline as pipeline

from PIL import Image, ImageFilter
from modules.util import resample_image, set_image_shape_ceil


inpaint_head = None


class InpaintHead(torch.nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.head = torch.nn.Parameter(torch.empty(size=(320, 5, 3, 3), device='cpu'))

    def __call__(self, x):
        x = torch.nn.functional.pad(x, (1, 1, 1, 1), "replicate")
        return torch.nn.functional.conv2d(input=x, weight=self.head)


current_task = None


def box_blur(x, k):
    x = Image.fromarray(x)
    x = x.filter(ImageFilter.BoxBlur(k))
    return np.array(x)


def max33(x):
    x = Image.fromarray(x)
    x = x.filter(ImageFilter.MaxFilter(3))
    return np.array(x)


def morphological_open(x):
    x_int32 = np.zeros_like(x).astype(np.int32)
    x_int32[x > 127] = 256
    for _ in range(32):
        maxed = max33(x_int32) - 8
        x_int32 = np.maximum(maxed, x_int32)
    return x_int32.clip(0, 255).astype(np.uint8)


def up255(x, t=0):
    y = np.zeros_like(x).astype(np.uint8)
    y[x > t] = 255
    return y


def imsave(x, path):
    x = Image.fromarray(x)
    x.save(path)


def regulate_abcd(x, a, b, c, d):
    H, W = x.shape[:2]
    if a < 0:
        a = 0
    if a > H:
        a = H
    if b < 0:
        b = 0
    if b > H:
        b = H
    if c < 0:
        c = 0
    if c > W:
        c = W
    if d < 0:
        d = 0
    if d > W:
        d = W
    return int(a), int(b), int(c), int(d)


def compute_initial_abcd(x):
    indices = np.where(x)
    a = np.min(indices[0]) - 64
    b = np.max(indices[0]) + 65
    c = np.min(indices[1]) - 64
    d = np.max(indices[1]) + 65
    abp = (b + a) // 2
    abm = (b - a) // 2
    cdp = (d + c) // 2
    cdm = (d - c) // 2
    l = max(abm, cdm)
    a = abp - l
    b = abp + l
    c = cdp - l
    d = cdp + l
    a, b, c, d = regulate_abcd(x, a, b, c, d)
    return a, b, c, d


def solve_abcd(x, a, b, c, d, outpaint):
    H, W = x.shape[:2]
    if outpaint:
        return 0, H, 0, W
    while True:
        if b - a > H * 0.618 and d - c > W * 0.618:
            break

        add_h = (b - a) < (d - c)
        add_w = not add_h

        if b - a == H:
            add_w = True

        if d - c == W:
            add_h = True

        if add_h:
            a -= 1
            b += 1

        if add_w:
            c -= 1
            d += 1

        a, b, c, d = regulate_abcd(x, a, b, c, d)
    return a, b, c, d


def fooocus_fill(image, mask):
    current_image = image.copy()
    raw_image = image.copy()
    area = np.where(mask < 127)
    store = raw_image[area]

    for k, repeats in [(512, 2), (256, 2), (128, 4), (64, 4), (33, 8), (15, 8), (5, 16), (3, 16)]:
        for _ in range(repeats):
            current_image = box_blur(current_image, k)
            current_image[area] = store

    return current_image


class InpaintWorker:
    def __init__(self, image, mask, is_outpaint):
        a, b, c, d = compute_initial_abcd(mask > 0)
        a, b, c, d = solve_abcd(mask, a, b, c, d, outpaint=is_outpaint)

        # interested area
        self.interested_area = (a, b, c, d)
        self.interested_mask = mask[a:b, c:d]
        self.interested_image = image[a:b, c:d]

        # resize to make images ready for diffusion
        self.interested_image = set_image_shape_ceil(self.interested_image, 1024)
        H, W, C = self.interested_image.shape

        self.interested_mask = up255(resample_image(self.interested_mask, W, H), t=127)
        self.interested_fill = fooocus_fill(self.interested_image, self.interested_mask)

        # soft pixels
        self.mask = morphological_open(mask)
        self.image = image

        # ending
        self.latent = None
        self.latent_after_swap = None
        self.swapped = False
        self.latent_mask = None
        self.inpaint_head_feature = None
        return

    def load_latent(self,
                    latent_fill,
                    latent_inpaint,
                    latent_mask,
                    latent_swap=None,
                    inpaint_head_model_path=None):

        global inpaint_head
        assert inpaint_head_model_path is not None

        self.latent = latent_fill
        self.latent_mask = latent_mask
        self.latent_after_swap = latent_swap

        if inpaint_head is None:
            inpaint_head = InpaintHead()
            sd = torch.load(inpaint_head_model_path, map_location='cpu')
            inpaint_head.load_state_dict(sd)

        feed = torch.cat([
            latent_mask,
            pipeline.final_unet.model.process_latent_in(latent_inpaint)
        ], dim=1)

        inpaint_head.to(device=feed.device, dtype=feed.dtype)
        self.inpaint_head_feature = inpaint_head(feed)

        return

    def swap(self):
        if self.swapped:
            return

        if self.latent is None:
            return

        if self.latent_after_swap is None:
            return

        self.latent, self.latent_after_swap = self.latent_after_swap, self.latent
        self.swapped = True
        return

    def unswap(self):
        if not self.swapped:
            return

        if self.latent is None:
            return

        if self.latent_after_swap is None:
            return

        self.latent, self.latent_after_swap = self.latent_after_swap, self.latent
        self.swapped = False
        return

    def color_correction(self, img):
        fg = img.astype(np.float32)
        bg = self.image.copy().astype(np.float32)
        w = self.mask[:, :, None].astype(np.float32) / 255.0
        y = fg * w + bg * (1 - w)
        return y.clip(0, 255).astype(np.uint8)

    def post_process(self, img):
        a, b, c, d = self.interested_area
        content = resample_image(img, d - c, b - a)
        result = self.image.copy()
        result[a:b, c:d] = content
        result = self.color_correction(result)
        return result

    def visualize_mask_processing(self):
        return [self.interested_fill, self.interested_mask, self.image, self.mask]

