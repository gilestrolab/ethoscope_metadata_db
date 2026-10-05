from setuptools import setup, find_packages

setup(
    name='ethoscope_metadata_db',
    version='2.0.0.dev0',
    packages=find_packages(exclude=['tests']),
    include_package_data=True,
    package_data={
        'ethoscope_metadata_db.web': ['templates/*.html', 'static/*'],
    },
    python_requires='>=3.11',
    install_requires=[
        'pandas>=1.5',
        'openpyxl>=3.0',
        'xlrd>=2.0',
    ],
    extras_require={
        'web': ['fastapi>=0.110', 'uvicorn>=0.29', 'jinja2>=3.1', 'python-multipart', 'python-dotenv'],
        'server': ['bottle'],  # the deprecated v1 server
        'test': ['pytest', 'xlwt', 'httpx'],
    },
    entry_points={
        'console_scripts': [
            'ethocensus=ethoscope_metadata_db.census.cli:main',
            'ethocensus-web=ethoscope_metadata_db.web.app:main',
        ],
    },
)
