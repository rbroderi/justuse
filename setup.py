import os
from pathlib import Path

from setuptools import find_packages, setup

here = os.path.abspath(os.path.dirname(__file__))
src = os.path.join(here, "src/use")

# Instead of doing the obvious thing (importing 'use' directly and just reading '__version__'),
# we are parsing the version out of the source AST here, because if the user is missing any
# dependencies at setup time, an import error would prevent the installation.
# Two simple ways to verify the installation using this setup.py file:
#
#     python3 setup.py develop
#
#  or:
#
#    python3 setup.py install
#
import ast

with open(os.path.join(src, "__init__.py")) as f:
    mod = ast.parse(f.read())
    version = [
        t
        for t in [*filter(lambda n: isinstance(n, ast.Assign), mod.body)]
        if t.targets[0].id == "__version__"
    ][0].value.value

meta = {
    "name": "justuse",
    "license": "MIT",
    "url": "https://github.com/amogorkon/justuse",
    "version": version,
    "author": "Anselm Kiefner",
    "author_email": "justuse-pypi@anselm.kiefner.de",
    "python_requires": ">=3.10",
    "keywords": [
        "installing",
        "packages",
        "hot reload",
        "auto install",
        "aspect oriented",
        "version checking",
        "functional",
    ],
    "classifiers": [
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3 :: Only",
    ],
    "extras_require": {"test": ["pytest", "pytest-cov", "pytest-env"]},
    "fullname": "justuse",
    "dist_files": ["pytest.ini", "tests/pytest.ini"],
    "description": "a pure-python alternative to import",
    "maintainer_email": "justuse-pypi@anselm.kiefner.de",
    "maintainer": "Anselm Kiefner",
    "platforms": ["any"],
    "download_url": "https://github.com/amogorkon/justuse/" "archive/refs/heads/main.zip",
}


requires = (
    "beartype( >= 0.8.1)",
    "furl(>= 2.1.2)",
    "icontract(>= 2.5.4)",
    "jinja2",
    "packaging(>= 21.0)",
    "pip",
    "pydantic(>= 1.8.2)",
    "requests",
    "wheel",
)


LONG_DESCRIPTION = Path("README.md").read_text()
setup(
    packages=find_packages(where="src"),
    package_dir={
        "": "src",
    },
    package_name="use",
    include_package_data=True,
    long_description=LONG_DESCRIPTION,
    long_description_content_type="text/markdown",
    requires=requires,
    install_requires=requires,
    setup_requires=requires,
    zip_safe=True,
    **meta
)
