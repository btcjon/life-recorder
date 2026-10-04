# UI v2 component inventory

Private licensed source: `/Users/jonbennett/Library/CloudStorage/Dropbox/Projects/shadcndashboard/ReactJS/main`. Product source was not modified. Copied files live in ignored `web/vendor/shadcn/`. Hashes are in `web/vendor/SHA256SUMS`. Adapted theme/utility styles also remain private there, as `css/life-recorder-theme.css` and `css/life-recorder-utilities.css`. Do not stage vendor source or `web/dist`. Public source cannot build this optional premium viewer without the owner's private component closure.

## Provenance

| Vendor path | SHA-256 | Source |
| --- | --- | --- |
| components/ui/alert.tsx | 4e02c1c568b9728f2ab489ff1596e86b59e4c2b7aa5c0bd9e132029ce8a30291 | src/components/ui/alert.tsx |
| components/ui/badge.tsx | d4352c0ecf8816cd379bf4a2a195a0a61df80463ba0ead2c75e2cba5107850e2 | src/components/ui/badge.tsx |
| components/ui/button.tsx | 3d8cade75504fbf7eea7ae742d92224ba2161ecd8b4d0ed9d689c4073720f890 | src/components/ui/button.tsx |
| components/ui/card.tsx | 58c4c2b928de61dc0d58bd243247efcbebe04e71bb4354817344dfe869bcb2d8 | src/components/ui/card.tsx |
| components/ui/checkbox.tsx | 3934c0b0aeaa981e7eb8f3ad94a4b0590f79e329f86a40a3f910e6e4cf37e024 | src/components/ui/checkbox.tsx |
| components/ui/dialog.tsx | ecc28c3a446a4f3f995cfa4fb24aac919f13871f9cd662b99a44a11a97ebdf07 | src/components/ui/dialog.tsx |
| components/ui/empty.tsx | cadaf4532265f24f26219ee9b394941bbb7819843851ca4d16cc939a0f2ce54b | src/components/ui/empty.tsx |
| components/ui/input.tsx | 72738f9383a3487c8531dc466b4923d3a0e4caf48d6774becf1cd0fc0877e662 | src/components/ui/input.tsx |
| components/ui/label.tsx | ce24a4c6dbf6b614b27f9bcce83d8873eb8035989ef1196ca126ff8107ac81b7 | src/components/ui/label.tsx |
| components/ui/popover.tsx | 118b3304d1e35ea0f72b9bf81effaf399100c1b73f7147b82fc8c0a6341a17f9 | src/components/ui/popover.tsx |
| components/ui/progress.tsx | b93e87f84651d270fc014a84b75742b461a0f01dcd267a976c9be8f788d5a162 | src/components/ui/progress.tsx |
| components/ui/resizable.tsx | 4eb48beafb9977d1ba59eed8c8dc70f264258eaad15676149b88ecd96ad39a68 | src/components/ui/resizable.tsx |
| components/ui/scroll-area.tsx | 98a32f13ddfb564bdd578c28c5f93f3414c244a9fd9a8e31d9a25dd2c3fedf8f | src/components/ui/scroll-area.tsx |
| components/ui/separator.tsx | 3d3675275580b621c20f10e1e327e01022a83a955cdeca0ce04b7f6d78d9364f | src/components/ui/separator.tsx |
| components/ui/sheet.tsx | 16af9122be4261fe11047fd1cbd4f5315a57b543cac6cdd0f2d19c1695fda71d | src/components/ui/sheet.tsx |
| components/ui/sidebar.tsx | 67f6363696b3baa8d251b4c427a23227cb2471bb319153ae4675f75b11ce7f38 | src/components/ui/sidebar.tsx |
| components/ui/skeleton.tsx | 87d5b3bb766c98f8ba59cc067d3bcdb4a4e8bc9f749a8be66fe85636983ef466 | src/components/ui/skeleton.tsx |
| components/ui/tabs.tsx | bec29f87f584bc772c25146fdae2d05cd0a725087c9302e01a907663c8b7696d | src/components/ui/tabs.tsx |
| components/ui/tooltip.tsx | b6676bfe2eca7eb5fab31ea3326c689227d58a84f5ba9b58f6337f56be955206 | src/components/ui/tooltip.tsx |
| css/globals.css | cd4bc74f4fce1f18dd91b7b009ce387068cc72dd3e260ac95cded4dbd1a3b5d8 | src/css/globals.css (reference; app uses a selective token copy) |
| css/styles/style-nova.css | bb33ca51d8250fe30165aa331af29aabe5a9f9c6dcbb2226dd9fe00c54b42680 | src/css/styles/style-nova.css (chosen variant) |
| hooks/use-mobile.ts | d2377fbad5ad2bdf7fa896a68c36ac8bf4aa74a974b2061f77b41ae58d623001 | src/hooks/use-mobile.ts |
| lib/utils.ts | 74a147991a119f1c4cdc25a4d9b22713632fa32eb136e55ab6939b006d71b9d6 | src/lib/utils.ts |
| reference/email/index.tsx | 551a072df9b4263dca1381768a9c2df8fc8bd66bc6ff9fd1db701ef47aa62d4e | email master/detail layout, not compiled |
| reference/email/email-list.tsx | f49b165d1d6781420951845bf4c0fe920986777c3e1a89218bfc17045f6fa516 | email list pattern, not compiled |
| reference/email/email-content.tsx | 56dea9630615d90245ba37c02bfbc1c43f0ed13e0eec5be88672aa9fd568317b | email detail pattern, not compiled |
| reference/layouts/Sidebar.tsx | 8bf30e662bdcfaad88bf30f67498c76db4850aa5dda953e87461cf648e239b37 | vertical sidebar layout, not compiled |

Fonts, copied into ignored `web/vendor/shadcn/fonts` and emitted by Vite under `/ui-v2/assets/`:

- `fonts/geist-sans/Geist-Variable.woff2` SHA-256 `affce75284deca6a9b57e425ee2cf3550e512982310473ec7ad6ce5083ea0a47`
- `fonts/geist-mono/GeistMono-Variable.woff2` SHA-256 `f2da1b1b4ebdc63fdf5ca847e8ed1b7c66eced0142e624d558af86cc26f53d5f`

## Runtime closure

Measured install from the locked ranges: React 19.3.0, Vite 8.3.2, TypeScript 6.0.3, Tailwind 4.3.3, `@base-ui/react` 1.8.0, `react-resizable-panels` 4.14.2, `lucide-react` 1.52.0, Vitest 4.1.11. ESLint 10 checks authored source and hooks; dependency audit reports zero vulnerabilities. No MSW, charts, AI, or demo contexts. Production build has no source maps. Vite emits the local font files under the `/ui-v2/` base.

`src/main.tsx` does not start a service worker. Route state uses search params. Local auth reads the URL fragment into `sessionStorage` key `life-recorder-token`, then clears the fragment and keeps the search string. Remote hosts do not store that fragment. The token is not written to `localStorage`.

## CSP browser acceptance

The Python-served production bundle supports CSSOM sizing for sidebar and resizable panels under `style-src 'self'`. Base UI also emits one fixed scrollbar rule. Only UI v2 permits that exact style-element SHA256; arbitrary injected styles remain blocked. Legacy CSP is unchanged. Desktop/mobile rendering and local fonts were checked in the browser. There is no blanket unsafe-inline/eval allowance.

## Public registry

`shadcn@latest info --json` succeeded. `view button` succeeded. `add button --dry-run` would overwrite `vendor/shadcn/components/ui/button.tsx`; it was not applied. `search badge` failed because this CLI requires an `@` registry namespace. Paid button source was left unchanged (hash above still matches).
