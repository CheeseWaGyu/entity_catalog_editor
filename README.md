# Entity Catalog Editor

Home Assistant のアドオンです。チェックリストで entity を選び、Muse Automation MCP
アドオンの `entity_catalog.yaml`（AI エージェントに見せてよい entity の許可リスト）を
更新します。各 entity には、エージェントにそのまま渡る `description`（説明文）と、
履歴の閲覧レベル `history.level`（off / summary / full）を付けられます。

## なぜ `all_addon_configs:rw` が必要なのか

編集対象の `entity_catalog.yaml` は、本アドオンではなく **Muse Automation MCP
アドオンの設定ディレクトリ**（`/addon_configs/<repo-hash>_muse_automation_mcp/`）に
あります。Home Assistant のアドオンが他アドオンの設定ディレクトリへ書くには
`all_addon_configs` マッピングが必須で、これ以外の方法では対象ファイルに届きません。
そのため map はこの形にしていますが、書き込み先として使うのは `catalog_path`
オプションで指定されたカタログファイル1つだけです。

## アクセス制御

- パネルは **`panel_admin: true`** で、Home Assistant の管理者だけに表示されます
  （MCP 側の承認パネルと同じ扱い）。カタログは「AI に何を見せ、何の履歴まで
  見せてよいか」を決める信頼境界なので、編集できる人を管理者に絞っています。
- アプリ自体に独自のログイン機構はありません。**Ingress 経由でのみ**開いてください。
  ポート（8099）はアドオンの `ports` には出しておらず、Supervisor 経由の
  Ingress アクセスだけを想定しています。
- 保存 API はサーバー側で entity_id の厳格な形式検証と、HA に実在するか
  既存カタログに載っているかの照合を行い、不正な ID は保存全体を 400 で拒否します。
  リクエストサイズ（1MB）と選択件数（1000件）にも上限があります。

## 注意が必要なドメイン

- `lock` / `cover` / `alarm_control_panel`: 物理的な影響があるため UI で警告します。
- `device_tracker` / `person`: 履歴が summary でも、滞在時間や遷移時刻から
  行動パターンが推測できるため、summary / full のどちらでもプライバシー注意を表示します。
- `camera`: 履歴 level が full のとき警告を表示します。
