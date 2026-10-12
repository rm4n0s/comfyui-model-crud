set -e
CU=$1; SP=$2
test -d "$CU/models/loras" && test -d "$SP"
rm -rf "$CU"/models/loras/*.safetensors "$CU/models/loras/sdxl" "$CU/models/loras/v2" "$SP/extra/loras" "$CU"/models/checkpoints/*
mkdir -p "$CU/models/loras/sdxl" "$CU/models/loras/v2" "$SP/extra/loras/v2" "$CU/models/checkpoints"
mk() { head -c $2 /dev/urandom > "$1"; }
mk "$CU/models/loras/detail_tweaker.safetensors" 171966
mk "$CU/models/loras/sdxl/Süß model 日本.safetensors" 2048
mk "$CU/models/loras/sdxl/lcm_lora_v10.safetensors" 5242880
mk "$CU/models/loras/v2/shared.safetensors" 1000
mk "$SP/extra/loras/v2/shared.safetensors" 2000
mk "$CU/models/loras/<img src=x onerror=window.__xss=1>\"'.safetensors" 100
mk "$CU/models/loras/b10.safetensors" 10; mk "$CU/models/loras/b2.safetensors" 20; mk "$CU/models/loras/B1.safetensors" 30
mk "$CU/models/checkpoints/sd15.safetensors" 4000; mk "$CU/models/checkpoints/old.ckpt" 3000
echo hi > "$CU/models/loras/notes.txt"
