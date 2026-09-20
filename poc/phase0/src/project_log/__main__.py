import os
from pathlib import Path

import uvicorn


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    secret_file = root / ".env.local"
    if secret_file.is_file():
        for line in secret_file.read_text().splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key == "GEMINI_API_KEY" and value.strip():
                # Never source a dotenv file as executable shell code.
                os.environ.setdefault(key, value.strip().strip("\"'"))
    uvicorn.run("project_log.api:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
