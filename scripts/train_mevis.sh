python3 main.py -c ./configs/mevis.yaml -rm train -ng 8 --epochs 30 \
--version "mevis" --lr_drop 20 -bs 1 -ws 8 --backbone "video-swin-t" \
--use_proxymmf \
-bpp "/opt/tiger/ljyaronld/pretrained/pretrained_swin_transformer/swin_tiny_patch244_window877_kinetics400_1k.pth"

