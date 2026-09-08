"""Build script for the Cython extension modules.

Project metadata lives in pyproject.toml; this file only declares the
compiled modules so that `pip install -e .` and `python -m build` produce
them. For a quick in-place build during development:

    python setup.py build_ext --inplace
"""

from Cython.Build import cythonize
from setuptools import Extension, setup

setup(
    include_package_data=False,
    exclude_package_data={"": ["*.c"]},
    ext_modules=cythonize(
        [Extension("operon.qc_module._parsers", ["operon/qc_module/_parsers.pyx"])],
        language_level=3,
    )
)
