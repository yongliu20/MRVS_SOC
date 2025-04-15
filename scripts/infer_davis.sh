python infer_davis.py -c ./configs/davis.yaml -rm test --version "davis_base_joint" -ng 8 --backbone "video-swin-b" \
-bpp "/opt/tiger/ljyaronld/pretrained/pretrained_swin_transformer/swin_base_patch244_window877_kinetics400_22k.pth" \
-ckpt "/opt/tiger/ljyaronld/SOC/base_joint/new_joint_base.tar"

sleep 30s
python eval_davis.py --results_path "/opt/tiger/ljyaronld/SOC/runs/davis/davis_base_joint/anno_0"
python eval_davis.py --results_path "/opt/tiger/ljyaronld/SOC/runs/davis/davis_base_joint/anno_1"
python eval_davis.py --results_path "/opt/tiger/ljyaronld/SOC/runs/davis/davis_base_joint/anno_2"
python eval_davis.py --results_path "/opt/tiger/ljyaronld/SOC/runs/davis/davis_base_joint/anno_3"