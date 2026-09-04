# ModWright

An MCP server that gives a coding agent the whole mod-authoring loop for C#
games: detect the install, scaffold a project, build it, deploy it where the
game actually loads mods from, and read the log afterwards.

The point is that none of those steps require the user to hand-edit a
`.csproj`, hunt for DLL paths, or copy files anywhere.

ModWright does not read the game's code. That job belongs to
[DecompilerServer](https://github.com/pardeike/DecompilerServer), which
ModWright calls for the one thing it needs from it: checking that the methods
a mod patches actually exist. Run both servers together and the agent can
answer "what does the game do" and "turn that into a working mod" in the same
session.

**v1 supports BepInEx 5** — Mono Unity games. Developed and verified in-game
against Lethal Company. IL2CPP games are refused explicitly rather than half
supported: their game logic is compiled to native code and is not there to
read.

---

## Requirements

| | |
|---|---|
| Python | 3.11+ |
| .NET SDK | any recent version, to build mods |
| .NET SDK 10 | only to build DecompilerServer |
| A game | with BepInEx 5 installed, or a mod-manager profile for it |

Mod managers are supported and expected: r2modman, Thunderstore Mod Manager
and Gale keep each profile as a standalone loader tree outside the game
folder, so for most players the game install is not where mods load from.

## Install

```sh
git clone https://github.com/slopefields/Modwright-MCP
cd Modwright-MCP
python -m venv .venv
.venv\Scripts\pip install -e .
```

That gives you `modwright-mcp` (in `.venv\Scripts\`).

Then build DecompilerServer somewhere alongside it:

```sh
git clone https://github.com/pardeike/DecompilerServer
cd DecompilerServer
dotnet build DecompilerServer.sln -c Release
```

## Registering the servers

Register **both**, as two independent processes. ModWright spawns its own
DecompilerServer over stdio rather than talking to your client's copy, so
registering DecompilerServer alone does not give ModWright access to it.

`.mcp.json` in a project directory (or the equivalent in your MCP client's own
config — this repo's copy is gitignored, since these are absolute paths on one
machine):

```json
{
  "mcpServers": {
    "modwright": {
      "type": "stdio",
      "command": "C:\\path\\to\\Modwright-MCP\\.venv\\Scripts\\modwright-mcp.exe",
      "args": [],
      "env": {
        "MODWRIGHT_DECOMPILER_PATH": "C:\\path\\to\\DecompilerServer\\bin\\Release\\net10.0\\DecompilerServer.exe"
      }
    },
    "decompiler": {
      "type": "stdio",
      "command": "C:\\path\\to\\DecompilerServer\\bin\\Release\\net10.0\\DecompilerServer.exe",
      "args": []
    }
  }
}
```

### `MODWRIGHT_DECOMPILER_PATH`

ModWright finds DecompilerServer through that environment variable and nothing else. 
There is no default and no search: guessing a path is worse than refusing one.

If it is unset, ModWright still works — builds succeed, deploys succeed — but
patch-name checking is silently not running. `build_mod` and `deploy_mod` then
report `patches_unchecked` with the reason instead of a `patches_checked`
count. **If a build reports no `patches_checked`, check this variable first.**

It must be set in the *registration's* `env` block. Setting it in your shell
does not reach a server your MCP client launches.

## Using it

Point an agent at a game and describe the mod. A typical first session:

1. `list_mod_profiles` — find where mods actually load from.
2. `detect_game` — confirm the game is supported.
3. `scaffold_mod_project` — with `deploy_root` set to the chosen profile.
4. Write the patch (this is where the decompiler earns its keep).
5. `deploy_mod` — builds, checks patch targets, places the DLL, and returns a
   `log_cursor`.
6. Launch the game, then `watch_mod_logs` with that `log_cursor` until
   `loader_restarted_since_cursor` turns true. Only then is the running game
   running this build.

Step 6 is the part worth understanding: a mod assembly is loaded **once**, when
the game process starts. A game left open through a redeploy keeps writing to
its log with the previous build in memory — fresh timestamps and all. So a log
newer than the deploy proves nothing on its own, and ModWright reports the
restart separately rather than letting a live-looking log stand in for one.

### `running_this_build` needs one setting turned on

That verdict is read from the loader's own record of which file it loaded and
when. BepInEx logs that on Harmony's `Info` channel, and **ships listening only
to `Warn, Error`** — so on a stock profile the log carries no such record and
the verdict comes back `null`.

`set_load_recording` adds `Info` to `[Harmony.Logger] LogChannels` in the
target's `BepInEx.cfg`. Call it once per deploy target; it applies from the
game's next start, and `enabled=False` puts it back. Nothing else in the file
is touched. The cost is a busier log, since the same channel also reports every
patch Harmony applies.

You do not have to know that in advance. Whenever `running_this_build` comes
back `null`, the response carries a `running_this_build_unknown` block naming
which of four situations it is — `loads_not_recorded`, `load_not_in_log`,
`load_time_unusable` or `nothing_deployed` — each with its own remedy.

One caveat worth knowing when reading the raw log yourself: Harmony writes that
stamp with a **12-hour** hour and no AM/PM marker, so `### At 2026-09-03
08.05.12` was written at either 08:05 or 20:05. ModWright resolves it against
the log's last-write time, and when both readings survive it reports the
alternative alongside rather than picking silently.

## Tools

**Setup**

| Tool | |
|---|---|
| `detect_game` | Identify which modding framework an install uses. |
| `list_mod_profiles` | List mod-manager profiles a mod could deploy into, including ones with no loader installed yet and what they need. |
| `scaffold_mod_project` | Create a buildable project wired to a specific install, with game references resolved. |
| `set_deploy_target` | Change where a project deploys to and reads logs from. |
| `set_game_install` | Point a project at the game on *this* machine — for a cloned mod, or a moved game. |
| `set_load_recording` | Make the loader record which file it loaded each plugin from, so `watch_mod_logs` can answer decisively. |

**Dependencies**

| Tool | |
|---|---|
| `list_available_mods` | List mods installed alongside this project, with full assembly paths — hand them to a decompiler to read what another mod does. |
| `add_mod_reference` | Compile against another installed mod, at the version the game will actually load. |
| `remove_mod_reference` | Stop compiling against one. |

**Build and run**

| Tool | |
|---|---|
| `validate_mod_patches` | Check every method the mod patches against the game's metadata. |
| `build_mod` | Compile, checking patch targets on the way. |
| `deploy_mod` | Build and place the artifact where the game loads mods from. |
| `watch_mod_logs` | Read the game's log from a cursor, with a diagnosis when something does not add up. |

Every tool returns a plain dict with `success`, and on failure a stable `code`
so an agent can branch without matching on message text.

### Why patch names get checked on every build

Harmony takes its target method name as a plain string. The compiler never
looks at it, so a typo — or a name a game update quietly renamed — builds
cleanly and fails only when the game launches. Checking runs after the build,
inside both `build_mod` and `deploy_mod`, and never fails the build: an
unverified patch name still compiles and still deploys. It costs a few seconds
against a build/deploy/launch cycle measured in minutes, and nothing at all for
a mod with no Harmony patches.

## What a scaffolded project looks like

| File | Committed | |
|---|---|---|
| `<ModName>.csproj` | yes | Targets net472 or netstandard2.1, matching the BCL the game actually ships. |
| `Plugin.cs` | yes | A BepInEx plugin that applies every `[HarmonyPatch]` in the assembly. |
| `.modwright.json` | yes | What the project *is*: game, framework, dependencies. No paths. |
| `.modwright.local.json` | **no** | The two fields that describe one person's disk. |
| `Modwright.props` | no | Generated reference paths. |

The split is deliberate. A mod repo pushed to a public host must not publish
where its author keeps their games — `deploy_root` alone would give away the
username, the mod manager in use, and the profile name. A clone gets the
project's identity and dependencies with no paths in it; one `set_game_install`
call makes it buildable again.

## Development

```sh
.venv\Scripts\python -m pytest
```

The suite runs with no game, no .NET SDK and no decompiler installed. Tests
that need any of those are marked `game`, `dotnet` and `decompiler`
respectively, and skip themselves when what they need is absent.