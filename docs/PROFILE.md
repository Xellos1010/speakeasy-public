# Deployment profile

Everything specific to *your* installation (hostnames, addresses, file paths, which account
file to use, and the **names** of your secrets) lives in a **deployment profile** that is kept
outside this repository. The repository ships safe defaults and works with no profile at all.

> Not to be confused with the voice/portable profile under `~/.speakeasy/profile/` (learned
> vocabulary and corrections). The deployment profile is configuration; that one is data.

## Where the profile is read from

1. `$SPEAKEASY_PROFILE`: a directory (the file `<dir>/profile.json` is used) or a path to a `.json` file.
2. `$XDG_CONFIG_HOME/speakeasy/profile/profile.json` (default `~/.config/speakeasy/profile/profile.json`).
3. Otherwise: built-in defaults.

Check what is active (prints no profile contents):

```bash
speakeasy deployment-profile path
speakeasy deployment-profile check
```

An invalid profile is ignored with a logged warning so the app still starts; `check` exits non-zero.

## Rules

- **No secret values, ever.** A profile holds names only: environment-variable names (pattern
  `^[A-Z][A-Z0-9_]{1,63}$`) or keychain item names. The schema rejects unknown keys, so a stray
  `password` field fails validation.
- `profile.json`, `/profile/`, `.speakeasy-profile/`, keystores, `local.properties`, `.env*` and
  `google-services.json` are in `.gitignore` so a local profile cannot be committed by accident.
- Precedence for each setting: explicit environment variable > settings file/CLI flag > profile > built-in default.

## Schema

Machine-readable: [`speakeasy/profile.schema.json`](../speakeasy/profile.schema.json) (JSON Schema
2020-12, `schema_version: 1`). Summary:

| Key | Type | Used by | Default when absent |
|---|---|---|---|
| `schema_version` | `1` (required) | loader | n/a |
| `profile_name` | string | display only | none |
| `server.public_url` | `https://host[:port]` | `speakeasy pair` QR payload | `tailscale status` DNS name, else a placeholder |
| `hosts.inference_ranking` | list of host ids | multi-host STT (`Settings.inference_ranking`) | `[]` (single host) |
| `hosts.host_addresses` | `{id: "host:port"}` | multi-host STT | `{}` |
| `accounts.accounts_file` | path | rewrite modes, canary, translate | `~/.claude-accounts.json` |
| `operator_overlay.id` / `.dir` | string / path | `speakeasy profile operator-overlay` | `default` / `~/.speakeasy/operator-profiles` |
| `android.server_url` | `https://host[:port]` | documentation of the Gradle setting `speakeasy.serverUrl` | empty (pair by QR) |
| `android.keystore_path`, `android.key_alias` | string | documents `speakeasy.keystore`, `speakeasy.keyAlias` | see Android section |
| `android.store_password_env`, `android.key_password_env` | env var **name** | documents `speakeasy.storePasswordEnv` | `SPK_KS_PW` |
| `packaging.bundle_id` | string | `setup.py` CFBundleIdentifier | `org.speakeasy.dictation` |
| `packaging.sign_identity`, `.sign_keychain`, `.sign_keychain_password_env` | string / path / env **name** | `scripts/package.sh` (export as `SPEAKEASY_SIGN_*`) | ad-hoc signing |
| `secrets.<label>` | `{source: env\|keychain\|file, name}` | documentation of where each secret lives | none |

Minimal example:

```json
{
  "schema_version": 1,
  "server": { "public_url": "https://my-mac.my-tailnet.example:8445" },
  "hosts": { "inference_ranking": ["linux-box"], "host_addresses": { "linux-box": "192.0.2.7:7420" } },
  "android": { "store_password_env": "SPK_KS_PW" },
  "secrets": { "android-keystore": { "source": "keychain", "name": "speakeasy-android-keystore" } }
}
```

## Android build settings

`android/app/build.gradle.kts` looks each setting up by name: Gradle property
(`-P` / `~/.gradle/gradle.properties`) > `android/local.properties` (gitignored; copy
`android/local.properties.example`) > environment variable.

| Setting | Env fallback | Meaning |
|---|---|---|
| `speakeasy.serverUrl` | `SPEAKEASY_SERVER_URL` | Default server URL in `BuildConfig` (empty = scan the pairing QR) |
| `speakeasy.keystore` | `SPEAKEASY_KEYSTORE` | Release keystore path (default `keystore/speakeasy-release.jks`) |
| `speakeasy.keyAlias` | `SPEAKEASY_KEY_ALIAS` | Key alias (default `speakeasy`) |
| `speakeasy.storePasswordEnv` | none | **Name** of the env var holding the keystore password (default `SPK_KS_PW`) |

Release builds are signed only when the keystore file exists and that env var is set. Keystores,
passwords and `google-services.json` are never committed. The app does not use Firebase.

## Setting up your own

Use the template repository (an interactive-free `scripts/setup.sh` copies an example profile into
`~/.config/speakeasy/profile/` and validates it), or write the file by hand from the example above.
