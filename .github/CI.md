# Repository CI

`CI` workflow は全 Pull Request、`main` への push、手動実行を対象とする。
Required Check として指定できる job 名は `Repository checks` に固定する。
Ruleset の変更はこの workflow の追加には含めない。

現在の文書中心の Repository では以下を検証する。

- Git 管理対象の Markdown / YAML / Python / text と主要設定ファイルの末尾空白、merge conflict marker
- Markdown のリンク・画像・参照リンクが指す Git 管理対象ファイルまたはその親ディレクトリの存在
- YAML の構文、重複した mapping key、安全な読み取り
- 検証スクリプトが正常な文書を許容し、破損した入力を検出する回帰テスト

Markdown の行末の 2 個以上のスペースによる改行は許容する。
conflict marker の検出対象は、行頭で `<` / `=` / `>` / `|` の同一記号が 7 文字以上連続し、空白または行末が続く場合とする。
`=======` は conflict marker として拒否するが、Markdown の Setext 見出しとして解析された実際の下線行は許容する。
リンク検証は Markdown の構文として書かれたリンクを対象とし、コード例中のリンクは除外する。
先頭が `/` のリンクは、[GitHub の文書表示仕様](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#relative-links)に従い Repository root から解決する。
存在していても未追跡ファイル、`.git/`、生成された `__pycache__/` 等へのリンクは拒否する。
symlink のリンク先も Git 管理対象である必要がある。
外部 URL への通信、見出し fragment、HTML のリンク属性は検証対象外とする。
YAML は mapping の merge 展開を 10,000 entries 以下に制限し、循環する merge は拒否する。
さらに、merge を含む mapping の展開項目数の累計を YAML ファイル全体で 100,000 entries 以下に制限する。
複数 document を含むファイルでも累計はリセットせず、上限超過となる mapping の展開前に拒否する。
これは merge の指数展開を抑える制限であり、通常の sequence alias は参照を共有するため対象外とする。
Application の build / format / lint / test はコード構成の確定後に追加する（PAW-004）。

GitHub Actions と Git の pre-commit hook は、[.pre-commit-config.yaml](../.pre-commit-config.yaml) の同じ hook を実行する。
共通 entry の [run_ci.py](scripts/run_ci.py) は回帰テストと Repository 検証を順に実行し、どちらかの失敗を commit 拒否として返す。
検証ライブラリの version は hook の `additional_dependencies` に固定し、pre-commit が専用環境へ導入する。

Python 3.13 以上で初回セットアップする場合:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r .github/requirements-ci.txt
.venv/bin/python .github/scripts/install_hooks.py
.venv/bin/python -m pre_commit run --all-files
```

Python の `venv` / `ensurepip` が利用できず、`uv` が導入済みの場合:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r .github/requirements-ci.txt
.venv/bin/python .github/scripts/install_hooks.py
.venv/bin/python -m pre_commit run --all-files
```

初回の依存導入にはネットワーク接続が必要となる。
[installer](scripts/install_hooks.py) は `core.hooksPath` や既存の `pre-commit` / `pre-commit.legacy` がある場合、その設定を上書きせず停止する。
依存環境の準備に成功してから既存 hook の有無を再確認し、Git hook を作成する。依存導入が失敗した場合は hook を残さず、同じ installer で再試行できる。
linked worktree の hook は元 Repository と共有するため、インストールに使う `.venv` は削除予定の一時ディレクトリではなく、継続利用する場所へ置く。
通常の `git commit` では、[pre-commit の仕様](https://pre-commit.com/#pre-commit)に従い未stageの変更を一時退避し、stage済みの内容で全検証を実行してから復元する。
`--no-verify` 等の Git の明示的な回避機能まで禁止する仕組みではない。

hook を外す場合は、導入に使った環境で `.venv/bin/python -m pre_commit uninstall` を実行する。
GitHub の CI は引き続き実行される。

検証対象は `git ls-files` で取得するため、新規ファイルも検証する場合は先に stage する。
CI は Repository の読み取り権限のみを持ち、Secret を参照しない。
