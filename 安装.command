#!/bin/zsh
cd "$(dirname "$0")" && /usr/bin/python3 install_hooks.py install
echo
read "?按回车关闭这个窗口"
