import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    config = Path(__file__).parent / ".env"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip())
    uvicorn.run("poc.server:create_app", factory=True, host=os.environ.get("BIND_HOST", "127.0.0.1"),
                port=8000, workers=1, access_log=False)
