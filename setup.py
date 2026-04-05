from setuptools import setup

APP = ["lichess_menubar.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Lichess Tracker",
        "CFBundleDisplayName": "Lichess Tracker",
        "CFBundleIdentifier": "dev.joulev.lichess-tracker",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": True,  # hide from Dock (menu-bar-only app)
    },
    "packages": ["chess", "requests", "certifi", "charset_normalizer", "idna", "urllib3"],
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
