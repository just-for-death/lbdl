import sys
from pathlib import Path

# Allow `import app` when running pytest from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
