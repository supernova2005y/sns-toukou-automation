"""`python -m sns_post` で起動する（sns-post.exe が使えない環境向け）。"""
import sys

from .cli import main

sys.exit(main())
