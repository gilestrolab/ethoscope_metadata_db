from setuptools import setup, find_packages

setup(
    name='ethoscope_metadata_db',
    version='2.0.0.dev0',
    packages=find_packages(exclude=['tests']),
    include_package_data=True,
    python_requires='>=3.10',
    install_requires=[
        'pandas>=1.5',
        'openpyxl>=3.0',
        'xlrd>=2.0',
    ],
    extras_require={
        'server': ['bottle'],
        'test': ['pytest', 'xlwt'],
    },
    entry_points={
        'console_scripts': [
            'ethocensus=ethoscope_metadata_db.census.cli:main',
        ],
    },
)
