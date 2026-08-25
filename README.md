# Open Agent OS by OpenIT

[![Open Agent OS](https://img.shields.io/badge/Open%20Agent%20OS-by%20OpenIT-0A66C2?style=for-the-badge)](https://github.com/openit-mykim/open-agent-os)
[![Fork](https://img.shields.io/badge/fork-deepseek--ai%2Fdeepseek--harness-24292e?logo=github)](https://github.com/deepseek-ai/deepseek-harness)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Cordis](https://img.shields.io/badge/powered%20by-Cordis-7c3aed)](https://github.com/cordiverse/cordis)

English | [中文](README.zh.md)

> **Open Agent OS**는 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`, MIT)를 포크하여 **OpenIT만의 Agent OS로 제품화**하는 프로젝트입니다.
> 본 저장소는 `openit-mykim/open-agent-os` 에서 독립적으로 개발·배포되며, 업스트림 `deepseek-ai/deepseek-harness` 변경사항은 선택적으로 병합합니다.

## Purpose — 왜 Open Agent OS인가

- **제품화**: DeepSeek Harness의 `everything is a plugin` + [Cordis](https://github.com/cordiverse/cordis) 아키텍처를 유지하되, OpenIT 서비스/인프라/보안 요구에 맞춘 기본 플러그인·정책·배포 파이프라인을 탑재
- **독립 브랜딩/배포**: npm/배포 네이밍을 `open-agent-os` 로 정리하고, OpenIT 전용 문서·가이드·운영 도구를 축적
- **업스트림 존중**: 원저작권과 MIT 라이선스를 그대로 유지하고, 개선사항은 업스트림 기여로 환류

## Origin & License

- **Upstream**: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) — Copyright (c) 2026 DeepSeek, [MIT](LICENSE)
- **This fork**: [openit-mykim/open-agent-os](https://github.com/openit-mykim/open-agent-os) — OpenIT이 유지·배포. 라이선스는 **MIT 유지**. 원본 저작권 고지를 삭제하지 않습니다.
- Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

DeepSeek Harness (`dsh`) is an open-source agent harness developed by [DeepSeek AI](https://deepseek.com). It uses an architecture where **everything is a plugin**, powered by [Cordis](https://github.com/cordiverse/cordis) — see [_A Programming Paradigm for Spatiotemporal Composability_](https://github.com/cordiverse/paper).

## Developer preview

DeepSeek Harness is currently in _developer preview_ and is iterating rapidly. **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**

## Run

### Run from `npm`

Install `Node.js`, then run:

```sh
npx @deepseek-ai/dsh web
```

The command starts the Web UI at `http://127.0.0.1:3080` by default and opens it in the default browser for a local launch. An SSH launch only prints the host URL because the SSH client or editor owns the local forwarded address. Pass `--no-open` to run the server without opening a browser. See [Web UI guide](docs/user/guide/index.md).

> Open Agent OS npm 배포 준비 후에는 `npx open-agent-os web` 형태로 제공 예정 — 업스트림 `dsh` 와 병행 사용 가능하도록 설계합니다.

### Run from source

```sh
git clone https://github.com/openit-mykim/open-agent-os.git
cd open-agent-os
pnpm install
pnpm run build
pnpm dsh web
```

`pnpm run build` prepares the repository artifacts. `pnpm dsh web` uses those built artifacts without rebuilding.

Upstream clone (원본 그대로 실행 시):

```sh
git clone https://github.com/deepseek-ai/deepseek-harness.git
```

## Community and support

- Upstream discussions: [deepseek-ai/deepseek-harness/discussions](https://github.com/deepseek-ai/deepseek-harness/discussions)
- Add the [`dsh-plugin`](https://github.com/topics/dsh-plugin) topic to your plugin repository for discoverability.
- Join <a href="https://discord.gg/Ycq5dCaS4">DeepSeek Harness Discord community</a>.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Open Agent OS 관련 이슈/PR은 본 저장소(`openit-mykim/open-agent-os`)에 제출해 주세요.

## Development

Start with the [development guide](docs/development.md) and [architecture documentation](docs/architecture.md).

For agents, follow [AGENTS.md](AGENTS.md).

## License

[MIT](LICENSE) — Copyright (c) 2026 DeepSeek (upstream). This fork retains the original copyright notice. Third-party dependencies and their licenses are disclosed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
