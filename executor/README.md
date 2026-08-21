# Executor runtime files

Döner downloads these on first use into `%LOCALAPPDATA%\Doener\exec`. They are
**not** shipped beside the exe and **not** embedded in it.

## Why downloaded rather than embedded

`QuorumAPI.dll` updates itself (`UseAutoUpdateAPI`) whenever Roblox moves. A copy
baked into `Döner.exe` would be frozen at build time and, worse, would be written
back over the freshly updated DLL on the next launch — so the executor would
quietly rot after every Roblox update.

Döner fetches whatever is missing and then leaves both files alone, so Quorum's
own updater owns the DLL from that point on. The Executor tab has a **Reinstall**
button that deletes both, forcing a fresh download on the next attach.

## Files

| Path | What it is |
|---|---|
| `DonerExec.exe` | Managed pipe helper that drives QuorumAPI |
| `QuorumAPI.dll` | Vendor assembly |
| `src/` | Source for the helper, so it can be rebuilt |

To publish a new helper, build it and replace `DonerExec.exe` here — no Döner
rebuild is needed, the download URL is stable.

## Why there is a helper at all

`QuorumAPI.dll` is a managed .NET assembly; Döner is native C++. Native code
cannot call it directly. Of the three ways across — hosting the CLR in-process,
a C++/CLI bridge, or a managed helper over IPC — this is the third, for reasons
beyond convenience:

- Quorum injects, spawns a console and self-updates. A fault in any of that would
  otherwise take down the process that owns the overlay's swap chain.
- `ExecuteScript` is an instance method taking a string. Native CLR hosting only
  reaches static entry points with a fixed delegate shape, so a managed shim was
  needed regardless.

## Protocol

One line in, one reply, connection closes after each. Pipe: `doener_exec`.

```
STATUS          -> OK attached=0|1
ATTACH          -> OK attached | ERR <reason>
EXEC <base64>   -> OK | ERR <reason>
KILL            -> OK
LOG             -> queued output lines, "." terminates
```

The script is base64-encoded so newlines survive the line protocol.

## Building

```
src\build.bat
```

Uses the C# compiler that ships with Windows
(`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`), so no .NET SDK is
required. That compiler predates `async Main`, ranges, using-declarations and
nullable annotations — hence the deliberately old-style C# in `Program.cs`.
