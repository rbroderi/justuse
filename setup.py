
import os
import sys

here = (os.path.abspath(os.path.dirname(__file__)))
src = os.path.join(here, "src/use")
sys.path.append(src)

from setuptools import find_packages
from setuptools import setup

meta={
    "name":"pyuse",
    "description":"A self-documenting, functional way to import modules in Python with advanced features.",
    "license":"MIT",
    "url":"https://github.com/amogorkon/pyuse",
    "version":"0.1.0",
    "author":"Anselm Kiefner",
    "author_email":"use-pypi@anselm.kiefner.de",
    "python_requires":">=3.8",
    "keywords":["import","reload"],
    "classifiers":[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3 :: Only",
    ]
}


with open("README.md") as f:
    LONG_DESCRIPTION = f.read()
  
setup(
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    package_name="use",
    long_description=LONG_DESCRIPTION,
    long_description_content_type="text/markdown",
    zip_safe=False,
    **meta
)
