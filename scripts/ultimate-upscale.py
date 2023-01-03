import math

import modules.scripts as scripts
import gradio as gr
from PIL import Image, ImageDraw

from modules import processing, shared, sd_samplers, images, devices
from modules.processing import Processed
from modules.shared import opts, cmd_opts, state

def getFactor(num):
    if num == 1:
        return 2
    if num % 4 == 0:
        return 4
    if num % 3 == 0:
        return 3
    if num % 2 == 0:
        return 2
    return 0

def upscale(p, init_img, upscaler_index, tileSize, padding):
    scale_factor = max(p.width, p.height) // max(init_img.width, init_img.height)
    print(f"Canva size: {p.width}x{p.height}")
    print(f"Image size: {init_img.width}x{init_img.height}")
    print(f"Scale factor: {scale_factor}")

    upscaler = shared.sd_upscalers[upscaler_index]

    p.extra_generation_params["SD upscale overlap"] = padding
    p.extra_generation_params["SD upscale upscaler"] = upscaler.name

    initial_info = None
    seed = p.seed

    upscaled_img = init_img
    if upscaler.name == "None":
        return upscaled_img.resize((p.width, p.height), resample=Image.LANCZOS)
    
    current_scale = 1
    iteration = 0
    current_scale_factor = getFactor(scale_factor)
    while current_scale_factor == 0:
        scale_factor += 1
        current_scale_factor = getFactor(scale_factor)
    while current_scale < scale_factor:
        iteration += 1
        current_scale_factor = getFactor(scale_factor // current_scale)
        current_scale = current_scale * current_scale_factor
        if current_scale_factor == 0:
            break
        print(f"Upscaling iteration {iteration} with scale factor {current_scale_factor}")
        upscaled_img = upscaler.scaler.upscale(upscaled_img, current_scale_factor, upscaler.data_path)

    return upscaled_img.resize((p.width, p.height), resample=Image.LANCZOS)


def redraw_image(p, upscaled_img, rows, cols, tileSize, padding):
    initial_info = None
    for yi in range(rows):
        for xi in range(cols):
            p.width = tileSize
            p.height = tileSize
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = padding
            mask = Image.new("L", (upscaled_img.width, upscaled_img.height), "black")
            draw = ImageDraw.Draw(mask)
            draw.rectangle((
                xi * tileSize,
                yi * tileSize,
                xi * tileSize + tileSize,
                yi * tileSize + tileSize
            ), fill="white")

            p.init_images = [upscaled_img]
            p.image_mask = mask
            processed = processing.process_images(p)
            initial_info = processed.info
            if (len(processed.images) > 0):
                upscaled_img = processed.images[0]

    return upscaled_img, initial_info


def redraw_middle_offset_image(p, upscaled_img, rows, cols, tileSize, padding, offset_pass_denoise, offset_pass_mask_blur):
    initial_info = None
    gradient = Image.linear_gradient("L")

    # gradient = gradient.resize((tileSize, tileSize//2), resample=Image.LANCZOS)

    row_gradient = Image.new("L", (tileSize, tileSize), "black")
    row_gradient.paste(gradient.resize((tileSize, tileSize//2), resample=Image.LANCZOS), (0, 0))
    row_gradient.paste(gradient.rotate(180).resize((tileSize, tileSize//2), resample=Image.LANCZOS), (0, tileSize//2))
    col_gradient = Image.new("L", (tileSize, tileSize), "black")
    col_gradient.paste(gradient.rotate(90).resize((tileSize//2, tileSize), resample=Image.LANCZOS), (0, 0))
    col_gradient.paste(gradient.rotate(270).resize((tileSize//2, tileSize), resample=Image.LANCZOS), (tileSize//2, 0))

    p.denoising_strength = offset_pass_denoise
    p.mask_blur = offset_pass_mask_blur

    for yi in range(rows-1):
        for xi in range(cols):
            p.width = tileSize
            p.height = tileSize
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = padding
            mask = Image.new("L", (upscaled_img.width, upscaled_img.height), "black")
            mask.paste(row_gradient, (xi*tileSize, yi*tileSize + tileSize//2))

            p.init_images = [upscaled_img]
            p.image_mask = mask
            processed = processing.process_images(p)
            initial_info = processed.info
            if (len(processed.images) > 0):
                upscaled_img = processed.images[0]

    for yi in range(rows):
        for xi in range(cols-1):
            p.width = tileSize
            p.height = tileSize
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = padding
            mask = Image.new("L", (upscaled_img.width, upscaled_img.height), "black")
            mask.paste(col_gradient, (xi*tileSize+tileSize//2, yi*tileSize))

            p.init_images = [upscaled_img]
            p.image_mask = mask
            processed = processing.process_images(p)
            initial_info = processed.info
            if (len(processed.images) > 0):
                upscaled_img = processed.images[0]

    return upscaled_img, initial_info


def seam_draw(p, upscaled_img, seam_pass_width, seam_pass_padding, seam_pass_denoise, padding, tileSize, cols, rows, mask_blur):
    p.denoising_strength = seam_pass_denoise
    p.mask_blur = mask_blur

    gradient = Image.linear_gradient("L")
    mirror_gradient = Image.new("L", (256, 256), "black")
    mirror_gradient.paste(gradient.resize((256, 128), resample=Image.LANCZOS), (0, 0))
    mirror_gradient.paste(gradient.rotate(180).resize((256, 128), resample=Image.LANCZOS), (0, 128))

    row_gradient = mirror_gradient.resize((upscaled_img.width, seam_pass_width), resample=Image.LANCZOS)
    col_gradient = mirror_gradient.rotate(90).resize((seam_pass_width, upscaled_img.height), resample=Image.LANCZOS)

    for xi in range(1, cols):
        p.width = seam_pass_width + seam_pass_padding * 2
        p.height = upscaled_img.height
        p.inpaint_full_res = True
        p.inpaint_full_res_padding = seam_pass_padding
        mask = Image.new("L", (upscaled_img.width, upscaled_img.height), "black")
        mask.paste(col_gradient, (xi * tileSize - seam_pass_width // 2, 0))

        p.init_images = [upscaled_img]
        p.image_mask = mask
        processed = processing.process_images(p)
        if (len(processed.images) > 0):
            upscaled_img = processed.images[0]
    for yi in range(1, rows):
        p.width = upscaled_img.width
        p.height = seam_pass_width + seam_pass_padding * 2
        p.inpaint_full_res = True
        p.inpaint_full_res_padding = seam_pass_padding
        mask = Image.new("L", (upscaled_img.width, upscaled_img.height), "black")
        mask.paste(row_gradient, (0, yi * tileSize - seam_pass_width // 2))

        p.init_images = [upscaled_img]
        p.image_mask = mask
        processed = processing.process_images(p)
        if (len(processed.images) > 0):
            upscaled_img = processed.images[0]
    return upscaled_img


class Script(scripts.Script):
    def title(self):
        return "Ultimate SD upscale"

    def show(self, is_img2img):
        return is_img2img

    def ui(self, is_img2img):
        info = gr.HTML(
            "<p style=\"margin-bottom:0.75em\">Will upscale the image to selected with and height</p>")
        gr.HTML("<p style=\"margin-bottom:0.75em\">Redraw options:</p>")
        with gr.Row():
            redraw_enabled = gr.Checkbox(label="Enabled", value=True)
            tileSize = gr.Slider(minimum=256, maximum=2048, step=64, label='Tile size', value=512)
            mask_blur = gr.Slider(label='Mask blur', minimum=0, maximum=64, step=1, value=8)
            padding = gr.Slider(label='Padding', minimum=0, maximum=128, step=1, value=32)
        upscaler_index = gr.Radio(label='Upscaler', choices=[x.name for x in shared.sd_upscalers],
                                value=shared.sd_upscalers[0].name, type="index")
        gr.HTML("<p style=\"margin-bottom:0.75em\">Seams fix (half tile offset pass):</p>")
        with gr.Row():
            offset_pass_enabled = gr.Checkbox(label="Enabled")
            offset_pass_denoise = gr.Slider(label='Denoise', minimum=0, maximum=1, step=0.01, value=0.25)
            offset_pass_mask_blur = gr.Slider(label='Mask blur', minimum=0, maximum=64, step=1, value=4)
            offset_pass_padding = gr.Slider(label='Padding', minimum=0, maximum=128, step=1, value=16)
        gr.HTML("<p style=\"margin-bottom:0.75em\">Seams fix (bands pass):</p>")
        with gr.Row():
            seam_pass_enabled = gr.Checkbox(label="Enabled")
            seam_pass_width = gr.Slider(label='Width', minimum=0, maximum=128, step=1, value=64)
            seam_pass_denoise = gr.Slider(label='Denoise', minimum=0, maximum=1, step=0.01, value=0.25)
            seam_pass_mask_blur = gr.Slider(label='Mask blur', minimum=0, maximum=64, step=1, value=4)
            seam_pass_padding = gr.Slider(label='Padding', minimum=0, maximum=128, step=1, value=32)
        gr.HTML("<p style=\"margin-bottom:0.75em\">Save options:</p>")
        with gr.Row():
            save_upscaled_image = gr.Checkbox(label="Upscaled", value=True)
            save_offset_path_image = gr.Checkbox(label="Offset pass", value=False)
            save_seam_path_image = gr.Checkbox(label="Bands path", value=False)

        return [info, tileSize, mask_blur, padding, seam_pass_enabled, seam_pass_width, seam_pass_denoise, seam_pass_mask_blur,
                seam_pass_padding, upscaler_index, save_upscaled_image, save_seam_path_image, redraw_enabled,
                offset_pass_enabled, offset_pass_denoise, offset_pass_padding, save_offset_path_image, offset_pass_mask_blur]

    def run(self, p, _, tileSize, mask_blur, padding, seam_pass_enabled, seam_pass_width, seam_pass_denoise, seam_pass_mask_blur,
            seam_pass_padding, upscaler_index, save_upscaled_image, save_seam_path_image, redraw_enabled,
            offset_pass_enabled, offset_pass_denoise, offset_pass_padding, save_offset_path_image, offset_pass_mask_blur):
        processing.fix_seed(p)
        p.extra_generation_params["SD upscale tileSize"] = tileSize
        p.mask_blur = mask_blur
        seed = p.seed

        initial_info = None

        init_img = p.init_images[0]
        init_img = images.flatten(init_img, opts.img2img_background_color)

        # Upscaling
        upscaled_img = upscale(p, init_img, upscaler_index, tileSize, padding)

        # Drawing
        devices.torch_gc()

        p.do_not_save_grid = True
        p.do_not_save_samples = True

        p.inpaint_full_res = False

        rows = math.ceil(p.height / tileSize)
        cols = math.ceil(p.width / tileSize)

        print(f"Tiles amount: {rows * cols}")
        print(f"Grid: {rows}x{cols}")
        print(f"Seam path: {seam_pass_enabled}")

        seams = 0
        if seam_pass_enabled:
            seams = rows - 1 + cols - 1
        state.job_count = ((rows * cols) if redraw_enabled else 0) + (seams if seam_pass_enabled else 0) + (
            (rows * (cols - 1) + (rows - 1) * cols) if offset_pass_enabled else 0)

        result_images = []
        result_image = upscaled_img
        if redraw_enabled:
            result_image, initial_info = redraw_image(p, upscaled_img, rows, cols, tileSize, padding)
        result_images.append(result_image)
        if save_upscaled_image:
            images.save_image(result_image, p.outpath_samples, "", seed, p.prompt, opts.grid_format, info=initial_info, p=p)

        if offset_pass_enabled:
            print(f"Starting offset pass drawing")
            result_image, initial_info = redraw_middle_offset_image(p, result_image, rows, cols, tileSize, offset_pass_padding, offset_pass_denoise, offset_pass_mask_blur)
            result_images.append(result_image)
            if save_offset_path_image:
                images.save_image(result_image, p.outpath_samples, "", seed, p.prompt, opts.grid_format, info=initial_info, p=p)

        if seam_pass_enabled:
            print(f"Starting bands pass drawing")
            result_image = seam_draw(p, result_image, seam_pass_width, seam_pass_padding, seam_pass_denoise, padding, tileSize, cols, rows, seam_pass_mask_blur)
            result_images.append(result_image)
            if save_seam_path_image:
                images.save_image(result_image, p.outpath_samples, "", seed, p.prompt, opts.grid_format, info=initial_info, p=p)

        processed = Processed(p, result_images, seed, initial_info if initial_info is not None else "")

        return processed
