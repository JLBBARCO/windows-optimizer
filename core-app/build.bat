@echo off
setlocal

cd /d "%~dp0"

echo Installing build dependencies...
py -3 -m pip install --disable-pip-version-check pyinstaller cairosvg pillow

echo Generating icon from SVG...
py -3 -c "from pathlib import Path; import cairosvg; from PIL import Image; svg=Path('..\\src\\favicon\\favicon.svg').resolve(); png=Path('icon.png').resolve(); ico=Path('icon.ico').resolve(); cairosvg.svg2png(url=str(svg), write_to=str(png), output_width=256, output_height=256); Image.open(png).save(ico, format='ICO', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)]); png.unlink(missing_ok=True)"

echo Building executable...
py -3 -m PyInstaller ^
	--noconfirm ^
	--clean ^
	--onefile ^
	--name windows-optimizer ^
	--icon icon.ico ^
	--add-data "json;json" ^
	main.py

echo Build finished. Output: core-app\dist\windows-optimizer.exe
