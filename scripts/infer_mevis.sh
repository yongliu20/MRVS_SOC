python3 infer_mevis.py -c ./configs/mevis.yaml -rm test --version "mevis" -ng 1 --backbone "video-swin-t" \
-bpp "pretrained/pretrained_swin_transformer/swin_base_patch244_window877_kinetics400_1k.pth" \
-ckpt "SOC/mevis/mevis/mevis.tar"
