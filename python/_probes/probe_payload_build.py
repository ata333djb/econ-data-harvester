import gzip, json, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]  # 项目根：从本文件位置推导，不写死机器路径
CACHE = ROOT / "data" / "raw" / "_http_cache"
raw = (CACHE/"0aa788b698ee8e07.bin").read_bytes()
T = (gzip.decompress(raw) if raw[:2]==b"\x1f\x8b" else raw).decode("utf-8","replace")
print("="*78); print("getChartData 调用点上游 3200 字符（payload r 的拼装）"); print("="*78)
print(T[167800:171050])
