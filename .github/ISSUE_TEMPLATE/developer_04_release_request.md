---
name: Release Request
about: リリース申請・承認・実施 runbook
title: '[Release] vX.Y.Z Release Request'
labels: 'release'
assignees: ''
---

## リリース内容

### 概要

| 項目                     | 内容                                                |
| ---------------------- | ------------------------------------------------- |
| バージョン                  | `vX.Y.Z`                                          |
| リリース内容（GitHub Release） | <https://github.com/arayabrain/araya-optinist/releases/tag/vX.Y.Z> <br> (Draft: https://github.com/arayabrain/araya-optinist/releases/tag/untagged-xxxxxxxx) |
| リリース種別                 | {MAJOR \| MINOR \| PATCH}                             |
| リスクレベル                 | {Low \| Medium \| High}                               |
| 予定実施日時                 | YYYY-MM-DD (W) HH:MM - HH:MM                       |
| リリース担当者                | @                                                 |
| リリース作業支援者              | @                                                 |
| 承認者                    | @                                                 |
| QA担当者                  | ※リリース担当者/作業支援者 に同じ                                |
| オンコール担当者               | ※リリース担当者/作業支援者 に同じ                                |

### リリース対象

| 項目              | 内容        |
| --------------- | --------- |
| 対象コミットSHA       | ※SHA記入    |
| 付与するversion tag | `vX.Y.Z`  |

### 関連リンク

- [ ] Milestone: 全 Issue/PR が Close 済み・対象ブランチ(develop-main)へマージ済みであることを確認
  - {MILESTONE_URL}
- [ ] テスト結果:
  - System Test Cases (on the development env):
    - {SHEET_URL}
  - Release Test Cases (on the production env):
    - {SHEET_URL}
- [ ] メンテナンス告知: {告知ページ掲載 or メール通知}

---

## 影響範囲

### 変更概要

※このリリースの主題を1-2行で記載。

#### ユーザーへの影響

- [ ] 無料ユーザーへの影響: **{あり / なし}**
  - 影響内容:
- [ ] 課金ユーザーへの影響: **{あり / なし}**
  - 影響内容:
- [ ] Breaking change: **{あり / なし}**
  - 影響内容:
- [ ] 想定ダウンタイム: **{あり / なし}** (YYYY-MM-DD HH:MM - HH:MM JST)

#### 技術的影響

- DBマイグレーション: **{あり / なし}**
  - 内容:
- インフラ変更（AWS設定、IAM、シークレット等）: **{あり / なし}**
  - 内容:
- 外部依存サービスへの影響: **{あり / なし}**
  - 内容:
- 環境変数・設定ファイルの変更: **{あり / なし}**
  - 内容:

---

## ステータス

- 準備
  - [ ] 仮申請（計画段階・テスト前）
  - [ ] dev環境テスト中
  - [ ] Buffer（不具合対応中）
  - [ ] 申請（本申請・スケジュール確定後）
  - [ ] 承認
- リリース実施
  - [ ] リリース実施中
  - [ ] 本番テスト中（本番 Release test cases 実施中）
  - [ ] リリース完了・成功判定済み
- 後処理
  - [ ] クローズ

---

## リリース手順（リリース担当者用 runbook）

### 事前準備

#### リリース承認申請（リリース1週間～3日前）

- [ ] **1. 承認申請資料準備**
  - 担当: @
  - 日時:
    - 予定: {日時}
    - [ ] 実績: {日時}
  - [ ] milestone: 全 Issue/PR が close・develop-main へマージ済みを確認
  - [ ] リリース素材の準備完了
    - [ ] version file更新済 -> [DEPLOYMENT_PROCEDURE.md#release-preparation](https://github.com/arayabrain/araya-optinist/blob/develop-main/infrastructure/documentation/DEPLOYMENT_PROCEDURE.md#release-preparation)
  - [ ] pre-release: release notes / 対象 commit SHA を確認
  - [ ] test結果
    - [ ] dev テスト結果リンク（または省略根拠）を整理
    - [ ] Weekly Regression を対象コミット（pre-release tag）で実行（`@slow` e2e を含む。最大3時間、前日までに開始）
      ```bash
      # CI: unit / premium_lock / workflow_count / slow e2e をまとめて実行
      gh workflow run e2e.yml --ref vX.Y.Z
      gh run list --workflow e2e.yml -L 3

      # ローカルで @slow のみ流す場合
      cd frontend && RUN_SLOW=1 yarn test:e2e --grep @slow
      ```
      > `--ref` は branch か tag のみ。SHA は指定できないため pre-release tag 作成後に実行する。
      > 詳細は frontend/e2e/README.md の「The weekly regression, and running it on a branch」を参照。

- [ ] **2. 承認申請～承認**

- [ ] **3. ユーザーアナウンス（メンテ告知）**
  - ※基本的にはリリース承認後のアナウンス実施だが、承認者の仮承認により、先行アナウンスも可
  - 担当: @
  - 日時:
    - 予定: {日時}
    - 実績: {日時}
  - [ ] ユーザーリスト確認
  - [ ] BCCでメール配送

#### リリース準備（～リリース前日）

- [ ] **1. リリース手順の再確認**
  - 担当: 支援者
  - 日付:
    - 予定: {日時}
    - [ ] 実績: {日時}

- [ ] **2. メンテナンス作業環境の準備**
  - 担当: @
  - 日付:
    - 予定: {日時}
    - [ ] 実績: {日時}
  - [ ] AWS CLI 認証・対象アカウント確認
  - [ ] terraform backend が対象環境(prod)と一致を確認

### リリース作業 (MM/DD HH:MM - HH:MM)

> 各ステップ完了時に「時刻/実績」へ実時刻を記入。ダウンタイム見込みの進捗管理を兼ねる。

- [ ] **1. メンテナンス開始 / 作業開始**
  - 担当: @
  - 時刻:
    - 予定: {時刻}
    - [ ] 実績: {時刻}
  - [ ] Status を In progress に更新、Slack 連絡
  - [ ] 対象 commit SHA / タグ `vX.Y.Z` を再確認
- [ ] **2. デプロイ実施**
  - 担当: 支援者
  - 時刻:
    - 予定: {時刻}
    - [ ] 実績: {時刻}
  - [ ] terraform apply（インフラ / Lambda に変更がある場合。build script が terraform output を読むため必ず先に実施）
    - [ ] 実績: {時刻}
  - [ ] `infrastructure/scripts/ecr_build_push.sh` で image を build・push（frontend / studio に変更がある場合）
    - [ ] 実績: {時刻}
  - [ ] 全 ECS サービス（main / premium / public / background）を force-new-deployment で cycle
    - [ ] 実績: {時刻}
    - [ ] 全サービスが 1 deployment に収束したことを確認
      - check_ecs_image_drift.py を使用して、デプロイしたインスタンスが最新かの確認
        - [ ] 確認結果:
  <!-- - [ ] （DBマイグレーションがある場合）マイグレーション実行 -->
- [ ] **3. リリース後動作確認（主要EP / 認証 / 課金 / premium routing）**
  - 担当: @
  - 時刻:
    - 予定: {時刻}
    - [ ] 実績: {時刻}
  - [ ] リリーステストケースを実施（分担+E2E）
    - [ ] Priority:High を即時実施（合格が続行条件）
    - [ ] 主要ユースケース（ログイン / 無料 / 有料 / Public instance / premium 再割当て / Stripe）
    - [ ] 当日分を完了し、残り項目を約1日以内に実施・結果記録
- [ ] **4. リリース後監視（エラー率・メトリクス平常確認）**
  - 担当: オンコール
  - 時刻:
    - 予定: {時刻}
    - [ ] 実績: {時刻}
  - [ ] CloudWatch で 5xx 率・レイテンシ・主要メトリクス（最低30分）
    - [ ] 検証結果:
  - [ ] 異常時はロールバック基準で判断（30分以内）
- [ ] **5. リリース完了アナウンス**
  - 担当: @
  - 時刻:
    - 予定: {時刻}
    - [ ] 実績: {時刻}
  - [ ] メンテナンス終了 / リリース完了を告知
  - [ ] 完了報告コメントを issue に追加

### 事後作業

- [ ] **1. develop-main を main へマージ**
  - 担当: @
- [ ] **2. github milestone を close**
  - 担当: @
- [ ] **3. 本 issue（承認/手順）を close**
  - 担当: @

---

### 切り戻し手順

#### 切り戻し判断・アナウンス

- [ ] **1. 切り戻し判断**
  - 担当: @
- [ ] **2. 実施アナウンス**
  - 担当: @
  - [ ] 内部: Slack
  - [ ] 外部: 必要時ユーザー通知

<!-- 以下は実際に切り戻しを実施する場合にコメントアウトを解除して記入する。

#### 切り戻し判断基準（いずれか該当で切り戻し判断）

- [ ] 主要エンドポイントの 5xx エラー率が閾値（例: 平常時の 3 倍）を超過
- [ ] 認証・決済・課金フローでの失敗率増加
- [ ] premium instance 割当て / routing の失敗率増加
- [ ] データ整合性に関わる異常検知
- [ ] 再現可能な深刻な不具合の報告

#### 切り戻しステップ

- [ ] **3. 切り戻し実施**
  - 担当: 支援者
  > タスク定義は image を `:latest` で参照しているため、旧リビジョンへ戻しても image は戻らない。
  > 手順は INFRA_DEPLOYMENT_PROCEDURE.md の「Rollback to a Previous Image」に従う。
  - [ ] 切り戻し先の image tag を ECR で特定（`aws ecr list-images`）
  - [ ] 旧 image を `:latest` へ retag（`aws ecr put-image`）
  - [ ] terraform を旧構成へ apply（IAM / alarms / scheduler 含む）
  - [ ] （DBマイグレーションがある場合）ロールバック手順を実施
  - [ ] 全 ECS サービス（main / premium / public / background）を force-new-deployment で cycle
  - [ ] 全サービスが 1 deployment に収束したことを確認・ヘルスチェック
- [ ] **4. 動作確認**
  - 担当: @
  - [ ] 主要EP / 認証 / 課金 / premium routing のスモークテスト
  - [ ] 5xx 率・主要メトリクスが平常へ復帰を確認
- [ ] **5. 完了アナウンス**
  - 担当: @
  - [ ] 内部: Slack
  - [ ] 外部: 必要時
- [ ] **6. 事後対応**
  - [ ] 障害 analysis 用の issue を起票
  - [ ] 本 issue にインシデント発生・切り戻し実施を記録
  - [ ] 後日、振り返りミーティングを実施
-->

---

## リリース承認（承認者による確認）

### 承認チェックリスト

#### リリース内容

- [ ] GitHub Release（pre-release）の release notes が妥当である
- [ ] ユーザー影響のある change が明示されている
- [ ] 課金/無料ユーザーで影響が異なる場合、区別されている

#### リリース手順

- [ ] 各ステップ、所要時間、担当者が明記されている
- [ ] 切り戻しの判断者・実施担当者が明確である

#### Milestone

- [ ] 紐づく全issue/PRがclose済みである
- [ ] 紐づくPRが対象ブランチにマージ済みである
- [ ] 対象ブランチに意図しないコミットが混入していない

#### 対象ブランチ/コミットSHA/version tag

- [ ] version fileが更新されている（3ファイルすべて）
  - [pyproject.toml](https://github.com/arayabrain/araya-optinist/blob/develop-main/pyproject.toml) (`[tool.poetry] version`)
  - [frontend/package.json](https://github.com/arayabrain/araya-optinist/blob/develop-main/frontend/package.json) (`version`)
  - [docs/conf.py](https://github.com/arayabrain/araya-optinist/blob/develop-main/docs/conf.py) (`release`)
- [ ] 対象コミットSHAが明示されている
- [ ] pre-releaseのコミットSHAと一致している
- [ ] version tag名がバージョニングルールに従っている

#### テスト

- [ ] dev環境テスト結果: リスクレベルに応じた **System test cases (development)** が実施され、結果が記録されている
- [ ] テスト対象のコミットSHAがリリース対象と一致している
- [ ] 対象コミットで Weekly Regression が green である（unit / premium_lock / workflow_count / `@slow` e2e。実行できない場合は `make test_run_all`）
- [ ] System test cases を省略している場合、領域と省略根拠が手順シートに記載され妥当である
- [ ] Buffer（不具合対応・再テスト）が完了している
- [ ] 本番 Release test cases sheet の実施担当者・想定タイミングが手順シートに記載されている
<!-- - [ ] DBマイグレーションがある場合、development / staging で実行確認済み -->

#### 運用準備

- [ ] リスクレベルの評価が妥当である
- [ ] 必要な事前告知が実施済みである
- [ ] 作業日時・担当者が確定している
- [ ] 切り戻し手順がステージングで検証済み（High リスク時）

---

### 承認

- [ ] 承認者による承認: @
- 承認日時:

> 承認はこの issue へのコメントで `Approved` と記載することで行う。

---

## Close条件

この issue は以下の条件をすべて満たした場合に close する。

- [ ] リリース作業が完了している
- [ ] 本番 Release test cases sheet の全項目が pass している
- [ ] エラー率・メトリクスが平常時範囲内
- [ ] pre-release が正式 release に昇格済み
- [ ] リリース担当者が完了報告コメントを記載済み
- [ ] **Medium / High リスクの場合**: 承認者が完了確認を行い `Release confirmed` コメントを記載済み

| リスクレベル        | closeの実行者 |
| ------------- | --------- |
| Low           | リリース担当者   |
| Medium / High | 承認者       |
