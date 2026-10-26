# Catalogue (generated)

Generated from `registry/tools.json` (174 tools, built 2026-10-26T10:21:01+00:00). Do not edit by hand: run `scripts/build_registry.py build`.

Columns: **bin** = binary (`?` unknown; `*` curated, not upstream-verified) · **auto** = automation suitability · **ia** = interactive (none / optional / required).

## Search

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [fd](https://github.com/sharkdp/fd) | `fd` | cli | active | high | none | find-files |
| [igrep](https://github.com/konradsz/igrep) | `ig` | tui | active | none | required | search-content, interactive-search |
| [MeiliSearch](https://github.com/meilisearch/MeiliSearch) | `meilisearch` | service | active | high | none | search-engine |
| [qdrant-es-gateway](https://github.com/dstockton/qdrant-es-gateway) | `?` | service | experimental | unknown | none | search-engine |
| [repgrep](https://github.com/acheronfail/repgrep) | `rgr` | tui | active | none | required | search-replace, interactive-search |
| [ripgrep](https://github.com/BurntSushi/ripgrep) | `rg` | cli | active | high | none | search-content, find-files |
| [television](https://github.com/alexpasmantier/television) | `tv` | tui | active | low | required | fuzzy-select, interactive-search |
| [skim](https://github.com/skim-rs/skim) | `sk` | tui | active | medium | optional | fuzzy-select, fuzzy-filter |
| [scout](https://github.com/jhbabon/scout) | `scout` | tui | inactive | low | required | fuzzy-select |
| [Reflex](https://github.com/reflex-search/reflex) | `rfx` | cli | experimental | high | none | search-content, code-search-index |
| [sweep-rs](https://github.com/aslpavel/sweep-rs) | `sweep` | tui | active | low | required | fuzzy-select |

## FileSystem

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [diskus](https://github.com/sharkdp/diskus) | `diskus` | cli | active | high | none | dir-size |
| [broot](https://github.com/Canop/broot) | `broot` | tui | active | low | required | tree-view, file-manager, disk-usage |
| [dua-cli](https://github.com/Byron/dua-cli) | `dua` | cli | active | high | optional | disk-usage, dir-size |
| [dust](https://github.com/bootandy/dust) | `dust` | cli | active | medium | none | disk-usage |
| [dutree](https://github.com/nachoparker/dutree) | `dutree` | cli | inactive | medium | none | disk-usage |
| [eza](https://github.com/eza-community/eza) | `eza` | cli | active | medium | none | list-dir, tree-view |
| [felix](https://github.com/kyoheiu/felix) | `fx` | tui | active | none | required | file-manager |
| [fselect](https://github.com/jhspetersson/fselect) | `fselect` | cli | active | high | none | filesystem-sql, find-files |
| [httm](https://github.com/kimono-koans/httm) | `httm` | cli | active | medium | optional | snapshot-browse |
| [lscolors](https://github.com/sharkdp/lscolors) | `lscolors` | cli | active | high | none | color-paths |
| [lsd](https://github.com/lsd-rs/lsd) | `lsd` | cli | active | medium | none | list-dir, tree-view |
| [rip](https://github.com/nivekuil/rip) | `rip` | cli | inactive | medium | none | safe-delete |
| [spacedrive](https://github.com/spacedriveapp/spacedrive) | `?` | gui | active | none | required | file-manager |
| [trashy](https://github.com/oberblastmeister/trashy) | `trash` | cli | inactive | medium | none | safe-delete |
| [yazi](https://github.com/sxyazi/yazi) | `yazi` | tui | active | none | required | file-manager |
| [zoxide](https://github.com/ajeetdsouza/zoxide) | `zoxide` | cli | active | medium | optional | cd-jump |

## File

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [bat](https://github.com/sharkdp/bat) | `bat` | cli | active | high | none | view-file |
| [csvlens](https://github.com/YS-L/csvlens) | `csvlens` | tui | active | none | required | csv-view |
| [hck](https://github.com/sstadick/hck) | `hck` | cli | active | high | none | cut-fields |
| [hex](https://github.com/sitkevij/hex) | `hx` | cli | active | medium | none | hex-view |
| [hexyl](https://github.com/sharkdp/hexyl) | `hexyl` | cli | active | medium | none | hex-view |
| [intermodal](https://github.com/casey/intermodal) | `imdl` | cli | active | high | none | torrent-metainfo |
| [lemmeknow](https://github.com/swanandx/lemmeknow) | `lemmeknow` | cli | inactive | high | none | identify-strings |
| [Oculante](https://github.com/woelper/oculante) | `oculante` | gui | active | none | required | image-view |
| [ouch](https://github.com/ouch-org/ouch) | `ouch` | cli | active | high | none | archive |
| [runiq](https://github.com/whitfin/runiq) | `runiq` | cli | active | high | none | dedupe-lines |
| [ruplacer](https://github.com/your-tools/ruplacer) | `ruplacer` | cli | archived | high | none | search-replace |
| [termscp](https://github.com/veeso/termscp) | `termscp` | tui | active | low | required | file-transfer |
| [xcp](https://github.com/tarka/xcp) | `xcp` | cli | active | high | none | copy-files |
| [xplr](https://github.com/sayanarijit/xplr) | `xplr` | tui | active | low | required | file-manager, fuzzy-select |

## System Monitor

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [bandwhich](https://github.com/imsnif/bandwhich) | `bandwhich` | tui | active | medium | optional | network-bandwidth |
| [below](https://github.com/facebookincubator/below) | `below` | cli | active | medium | optional | system-monitor, resource-history |
| [bottom](https://github.com/ClementTsang/bottom) | `btm` | tui | active | none | required | system-monitor, process-list |
| [bpftop](https://github.com/jfernandez/bpftop) | `bpftop` | tui | active | none | required | ebpf-inspect |
| [diskonaut](https://github.com/imsnif/diskonaut) | `diskonaut` | tui | inactive | none | required | disk-usage |
| [featherbar](https://github.com/nim444/featherbar) | `?` | gui | active | none | required | system-monitor |
| [lazyslurm](https://github.com/hill/lazyslurm) | `lazyslurm` | tui | active | none | required | hpc-jobs |
| [netscanner](https://github.com/Chleba/netscanner) | `netscanner` | tui | active | none | required | network-scan, port-scan |
| [macchina](https://github.com/Macchina-CLI/macchina) | `macchina` | cli | active | medium | none | system-info |
| [macmon](https://github.com/vladkens/macmon) | `macmon` | tui | active | high | optional | power-monitor, system-monitor |
| [pik](https://github.com/jacek-kurlit/pik) | `pik` | tui | active | none | required | process-kill, process-list |
| [procs](https://github.com/dalance/procs) | `procs` | cli | active | medium | none | process-list |
| [rsftch](https://github.com/charklie/rsftch) | `rsftch` | cli | active | medium | none | system-info |
| [rustnet](https://github.com/domcyrus/rustnet) | `rustnet` | tui | active | low | required | network-monitor, packet-inspect |
| [RustScan](https://github.com/bee-san/RustScan) | `rustscan` | cli | active | high | none | port-scan |
| [sniffnet](https://github.com/GyulyVGC/sniffnet) | `sniffnet` | gui | active | none | required | network-monitor |
| [trippy](https://github.com/fujiapple852/trippy) | `trip` | tui | active | high | optional | traceroute |
| [ytop](https://github.com/cjbassi/ytop) | `ytop` | tui | archived | none | required | system-monitor |
| [Zenith](https://github.com/bvaisvil/zenith) | `zenith` | tui | active | none | required | system-monitor, process-list |
| [macdisp](https://github.com/noworrieseh/macdisp) | `macdisp` | cli | active | medium | none | display-config |

## Shell/Terminal

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [alacritty](https://github.com/alacritty/alacritty) | `alacritty` | gui | active | none | required | terminal-emulator |
| [atuin](https://github.com/atuinsh/atuin) | `atuin` | cli | active | medium | optional | shell-history |
| [createnv](https://github.com/cuducos/createnv) | `createnv` | cli | active | low | required | env-file |
| [handlr-regex](https://github.com/Anomalocaridid/handlr-regex) | `handlr` | cli | active | medium | none | xdg-open |
| [just](https://github.com/casey/just) | `just` | cli | active | high | none | task-runner |
| [mprocs](https://github.com/pvolok/dekit) | `dekit` | tui | active | low | required | multi-process |
| [navi](https://github.com/denisidoro/navi) | `navi` | tui | active | low | optional | cheatsheet |
| [nushell](https://github.com/nushell/nushell) | `nu` | shell | active | high | none | shell, structured-data |
| [rargs](https://github.com/lotabout/rargs) | `rargs` | cli | inactive | high | none | xargs-like |
| [rio](https://github.com/raphamorim/rio) | `rio` | gui | active | none | required | terminal-emulator |
| [rust-parallel](https://github.com/aaronriekenberg/rust-parallel) | `rust-parallel` | cli | active | high | none | parallel-exec |
| [sd](https://github.com/chmln/sd) | `sd` | cli | active | high | none | search-replace, text-transform |
| [shrs](https://github.com/MrPicklePinosaur/shrs) | `?` | library | inactive | none | none | shell-toolkit |
| [sparklebios](https://github.com/reactivepixels/sparklebios) | `bios` | cli | experimental | none | none | shell-splash |
| [splashboard](https://github.com/unhappychoice/splashboard) | `splashboard` | cli | experimental | none | none | shell-splash |
| [starship](https://github.com/starship/starship) | `starship` | cli | active | none | none | prompt |
| [tty7](https://github.com/l0ng-ai/tty7) | `?` | gui | experimental | none | required | terminal-emulator |
| [wezterm](https://github.com/wezterm/wezterm) | `wezterm` | gui | active | low | required | terminal-emulator, multiplexer |
| [zellij](https://github.com/zellij-org/zellij) | `zellij` | tui | active | low | required | multiplexer |

## Performance

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [cargo-flamegraph](https://github.com/flamegraph-rs/flamegraph) | `flamegraph` | cli | active | high | none | cpu-profile, flamegraph |
| [hyperfine](https://github.com/sharkdp/hyperfine) | `hyperfine` | cli | active | high | none | benchmark |
| [inferno](https://github.com/jonhoo/inferno) | `inferno-flamegraph` | cli | active | high | none | flamegraph |
| [py-spy](https://github.com/benfred/py-spy) | `py-spy` | cli | active | high | none | python-profile, cpu-profile |

## Dev-Utilities

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [bytehound](https://github.com/koute/bytehound) | `bytehound` | cli | inactive | medium | none | memory-profile |
| [delta](https://github.com/dandavison/delta) | `delta` | cli | active | low | none | diff-pager |
| [difftastic](https://github.com/Wilfred/difftastic) | `difft` | cli | active | medium | none | diff-structural |
| [dnspeep](https://github.com/jvns/dnspeep) | `dnspeep` | cli | inactive | medium | none | dns-observe |
| [dotter](https://github.com/SuperCuber/dotter) | `dotter` | cli | active | medium | none | dotfiles |
| [dtool](https://github.com/guoxbin/dtool) | `dtool` | cli | active | high | none | encoding-convert, hash |
| [edky](https://github.com/artob/edky) | `edky` | cli | experimental | high | none | key-convert |
| [eva](https://github.com/oppiliappan/eva) | `eva` | cli | active | high | optional | calculator |
| [fnm](https://github.com/Schniz/fnm) | `fnm` | cli | active | high | none | version-manager |
| [forge](https://github.com/tailcallhq/forgecode) | `forge` | agent | active | low | optional | ai-agent |
| [Forge](https://github.com/NorviaLabs/forge) | `?` | agent | experimental | low | required | ai-agent |
| [octomind](https://github.com/muvon/octomind) | `octomind` | agent | active | low | optional | ai-agent |
| [fw](https://github.com/brocode/fw) | `fw` | cli | active | medium | none | workspace-manager |
| [git-absorb](https://github.com/tummychow/git-absorb) | `git-absorb` | cli | active | medium | none | git-fixup |
| [gitlogue](https://github.com/unhappychoice/gitlogue) | `gitlogue` | tui | active | none | required | git-history-view |
| [gitoxide](https://github.com/GitoxideLabs/gitoxide) | `gix` | cli | active | medium | none | git-impl |
| [gitui](https://github.com/gitui-org/gitui) | `gitui` | tui | active | none | required | git-tui |
| [Gitside](https://github.com/dev-bhaskar8/gitside) | `gitside` | tui | experimental | none | required | git-tui |
| [gity](https://github.com/neul-labs/gity) | `gity` | daemon | experimental | low | none | git-accelerate |
| [gpg-tui](https://github.com/orhun/gpg-tui) | `gpg-tui` | tui | active | none | required | gpg-manage |
| [grex](https://github.com/pemistahl/grex) | `grex` | cli | active | high | none | regex-generate |
| [hurl](https://github.com/Orange-OpenSource/hurl) | `hurl` | cli | active | high | none | http-test, http-client |
| [Hyperconsciousness](https://github.com/louis030195/hyperconsciousness) | `hc` | cli | experimental | unknown | none | knowledge-store |
| [intentdiff](https://github.com/harivilasp/intentdiff) | `intentdiff` | cli | experimental | unknown | none | diff-structural, semantic-diff |
| [jaq](https://github.com/01mf02/jaq) | `jaq` | cli | active | high | none | json-query |
| [jj](https://github.com/jj-vcs/jj) | `jj` | cli | active | high | none | vcs |
| [jless](https://github.com/PaulJuliusMartinez/jless) | `jless` | tui | inactive | none | required | json-view |
| [lurk](https://github.com/JakWai01/lurk) | `lurk` | cli | active | high | none | syscall-trace |
| [mask](https://github.com/jacobdeichert/mask) | `mask` | cli | active | high | none | task-runner |
| [mise](https://github.com/jdx/mise) | `mise` | cli | active | high | none | version-manager, task-runner, dev-env-manager |
| [nika](https://github.com/supernovae-st/nika) | `nika-catalog-verify` | cli | experimental | unknown | none | workflow-engine |
| [onefetch](https://github.com/o2sh/onefetch) | `onefetch` | cli | active | high | none | git-info |
| [omnigraph](https://github.com/ModernRelay/omnigraph) | `omnigraph` | service | experimental | unknown | none | graph-db |
| [pastel](https://github.com/sharkdp/pastel) | `pastel` | cli | active | high | none | color-tool |
| [qsv](https://github.com/dathere/qsv) | `qsv` | cli | active | high | none | csv-query, csv-transform |
| [riff](https://github.com/walles/riff) | `riff` | cli | active | low | none | diff-pager |
| [rust_sqlite](https://github.com/joaoh82/rust_sqlite) | `sqlrite` | cli | experimental | none | none | embedded-db |
| [rust_kanban](https://github.com/yashs662/rust_kanban) | `rust-kanban` | tui | active | none | required | kanban |
| [sccache](https://github.com/mozilla/sccache) | `sccache` | cli | active | high | none | compile-cache |
| [sem](https://github.com/Ataraxy-Labs/sem) | `?` | cli | experimental | unknown | none | semantic-diff, diff-structural |
| [silicon](https://github.com/Aloxaf/silicon) | `silicon` | cli | inactive | high | none | code-image |
| [slumber](https://github.com/LucasPickering/slumber) | `slumber` | tui | active | medium | optional | http-client |
| [sqleibniz](https://github.com/xNaCly/sqleibniz) | `sqleibniz` | cli | experimental | medium | none | sql-lint |
| [steel](https://github.com/mattwparas/steel) | `steel` | cli | active | medium | optional | scheme-interpreter |
| [StyLua](https://github.com/JohnnyMorganz/StyLua) | `stylua` | cli | active | high | none | lua-format |
| [supercov](https://github.com/supercorp-ai/supercov) | `supercov` | cli | experimental | unknown | none | coverage |
| [tealdeer](https://github.com/tealdeer-rs/tealdeer) | `tldr` | cli | active | high | none | tldr |
| [tlrc](https://github.com/tldr-pages/tlrc) | `tldr` | cli | active | high | none | tldr |
| [tinty](https://github.com/tinted-theming/tinty) | `tinty` | cli | active | medium | none | theme-manager |
| [tokei](https://github.com/XAMPPRocky/tokei) | `tokei` | cli | active | high | none | code-stats |
| [Tura](https://github.com/Tura-AI/tura) | `?` | agent | experimental | unknown | none | ai-agent |
| [ugdb](https://github.com/ftilde/ugdb) | `ugdb` | tui | active | none | required | debugger-ui |
| [xsv](https://github.com/BurntSushi/xsv) | `xsv` | cli | archived | high | none | csv-query, csv-transform |

## Editor

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [amp](https://github.com/jmacdonald/amp) | `amp` | tui | active | none | required | edit-text |
| [gnvim](https://github.com/vhakulinen/gnvim) | `gnvim` | gui | active | none | required | edit-text |
| [helix](https://github.com/helix-editor/helix) | `hx` | tui | active | none | required | edit-text |
| [iota](https://github.com/gchp/iota) | `iota` | tui | inactive | none | required | edit-text |
| [lapce](https://github.com/lapce/lapce) | `lapce` | gui | active | none | required | edit-text |
| [neovide](https://github.com/neovide/neovide) | `neovide` | gui | active | none | required | edit-text |
| [PinkDown](https://github.com/3xian/PinkDown) | `?` | gui | active | none | required | edit-text, markdown-view |
| [qnote](https://github.com/Omibranch/qnote) | `?` | gui | unverified | none | required | edit-text, markdown-view |
| [rnote](https://github.com/flxzt/rnote) | `rnote` | gui | active | low | required | note-taking |
| [TUI-Journal](https://github.com/AmmarAbouZor/tui-journal) | `tjournal` | tui | active | none | required | note-taking |
| [xi-editor](https://github.com/xi-editor/xi-editor) | `?` | service | inactive | none | required | edit-text |
| [zed](https://github.com/zed-industries/zed) | `zed` | gui | active | none | required | edit-text |

## Others

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [bartib](https://github.com/nikolassv/bartib) | `bartib` | cli | active | medium | none | time-tracking |
| [coreutils](https://github.com/uutils/coreutils) | `coreutils` | cli | active | high | none | coreutils |
| [fend](https://github.com/printfn/fend) | `fend` | cli | active | high | optional | calculator, unit-convert |
| [gittype](https://github.com/unhappychoice/gittype) | `gittype` | tui | active | none | required | typing-game |
| [himalaya](https://github.com/pimalaya/himalaya) | `himalaya` | cli | active | medium | none | email |
| [inlyne](https://github.com/Inlyne-Project/inlyne) | `inlyne` | gui | active | none | required | markdown-view |
| [kanata](https://github.com/jtroo/kanata) | `kanata` | daemon | active | none | none | keyboard-remap |
| [md-tui](https://github.com/henriklovhaug/md-tui) | `mdt` | tui | active | none | required | markdown-view |
| [OpenTypeless](https://github.com/tover0314-w/opentypeless) | `?` | gui | experimental | none | required | voice-typing |
| [see](https://github.com/guilhermeprokisch/see) | `see` | cli | active | medium | none | view-file, markdown-view |
| [poketex](https://github.com/ckaznable/poketex) | `poketex` | tui | active | none | required | entertainment |
| [ratatui](https://github.com/ratatui/ratatui) | `?` | library | active | none | none | tui-library |
| [rustdesk](https://github.com/rustdesk/rustdesk) | `rustdesk` | gui | active | none | required | remote-desktop |
| [SubnetDesk](https://github.com/zibo-chen/SubnetDesk) | `?` | gui | experimental | none | required | remote-desktop |
| [screenpipe](https://github.com/screenpipe/screenpipe) | `screenpipe` | service | active | low | none | screen-record |
| [shadowsocks-rust](https://github.com/shadowsocks/shadowsocks-rust) | `sslocal` | service | active | medium | none | proxy |
| [sudo.rs](https://github.com/trifectatechfoundation/sudo-rs) | `sudo` | cli | active | none | none | privilege-escalation |
| [vaultwarden](https://github.com/dani-garcia/vaultwarden) | `vaultwarden` | service | active | low | none | password-server |
| [youtube-tui](https://github.com/Siriusmart/youtube-tui) | `youtube-tui` | tui | active | none | required | media |
| [ytermusic](https://github.com/ccgauche/ytermusic) | `ytermusic` | tui | active | none | required | media |
| [zola](https://github.com/getzola/zola) | `zola` | cli | active | high | none | static-site |
| [zerobrew](https://github.com/zerobrewhq/zerobrew) | `zb` | cli | experimental | medium | none | package-manager |

## Security

| tool | bin | kind | status | auto | ia | capabilities |
|---|---|---|---|---|---|---|
| [fnprint](https://github.com/1rhino2/fnprint) | `fnprint` | cli | experimental | unknown | none | binary-analysis |
| [n01d-forge](https://github.com/bad-antics/n01d-forge) | `?` | gui | experimental | none | required | disk-image-burn |
| [n01d-machine](https://github.com/bad-antics/n01d-machine) | `?` | gui | experimental | none | required | vm-manager |
