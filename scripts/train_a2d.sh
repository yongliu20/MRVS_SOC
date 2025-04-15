python3 main.py -c ./configs/a2d_sentences.yaml -rm train -ng 8 --epochs 40 \
--version "a2d" --lr_drop 20 -ws 8 -bs 2 --backbone "video-swin-t" \
--use_proxymmf \
-bpp "/opt/tiger/ljyaronld/pretrained/pretrained_swin_transformer/swin_tiny_patch244_window877_kinetics400_1k.pth"

