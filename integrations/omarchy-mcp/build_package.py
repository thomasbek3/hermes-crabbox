from pathlib import Path
import zipfile
root = Path(__file__).resolve().parent
with zipfile.ZipFile(root / 'skill.zip', 'w', compression=zipfile.ZIP_DEFLATED) as out:
    for path in sorted((root / 'skill').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            out.write(path, Path('omarchy-cloud-delegate') / path.relative_to(root / 'skill'))
