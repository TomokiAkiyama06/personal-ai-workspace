# Repository CI

`CI` workflow は全 Pull Request、`main` への push、手動実行を対象とする。
Required Check として指定できる job 名は `Repository checks` に固定する。
Ruleset の変更はこの workflow の追加には含めない。

現在の文書中心の Repository では以下を検証する。

- Git 管理対象の Markdown / YAML / Python / text と主要設定ファイルの末尾空白、merge conflict marker
- Markdown のリンク・画像・参照リンクが指す Repository 内のファイルまたはディレクトリの存在
- YAML の構文、重複した mapping key、安全な読み取り
- 検証スクリプトが正常な文書を許容し、破損した入力を検出する回帰テスト

Markdown の行末の 2 個以上のスペースによる改行は許容する。
リンク検証は Markdown の構文として書かれたリンクを対象とし、コード例中のリンクは除外する。
外部 URL への通信、見出し fragment、HTML のリンク属性は検証対象外とする。
Application の build / format / lint / test はコード構成の確定後に追加する（PAW-004）。

Python 3.13 でローカル実行する場合:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r .github/requirements-ci.txt
.venv/bin/python -m unittest discover -s .github/scripts -p 'test_*.py' -v
.venv/bin/python .github/scripts/check_repository.py
```

検証対象は `git ls-files` で取得するため、新規ファイルも検証する場合は先に stage する。
CI は Repository の読み取り権限のみを持ち、Secret を参照しない。
