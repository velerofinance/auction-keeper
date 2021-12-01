from distutils.core import setup

setup(
    name='auction-keeper',
    version='1.0.0',
    packages=[
        'auction_keeper',
    ],
    url='https://github.com/velerofinance/auction-keeper',
    license='',
    author='',
    author_email='',
    description='',
    install_requires=[
        "pymaker==1.2.0",
        "pygasprice-client==1.0.*",
    ]
)