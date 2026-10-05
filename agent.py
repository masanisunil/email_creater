import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from email_sender import agent as implementation

if __name__ == "__main__":
    implementation.main()
else:
    sys.modules[__name__] = implementation