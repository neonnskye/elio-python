# Usage

## 1. Clone repository
```bash
git clone https://github.com/neonnskye/elio-python.git
cd elio-python/
git checkout cmd-merge
```

## 2. Download models
```bash
mkdir models/
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx?download=true -O models/en_US-hfc_female-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx.json?download=true -O models/en_US-hfc_female-medium.onnx.json
```

## 3. Setup Python
```bash
uv venv --python 3.13 --system-site-packages
source .venv/bin/activate
uv sync
```

## 4. Run scripts
```
uv run receiver.py
```