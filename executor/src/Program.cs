// DonerExec - the managed half of Doener's executor tab.
//
// Doener is native C++ and QuorumAPI is a managed assembly, so something has to
// stand between them. This is it: a headless pipe server that owns the
// QuorumModule and exposes five verbs to the overlay.
//
// It lives in its own process on purpose. QuorumAPI injects into Roblox, spawns
// a console and self-updates; hosting that inside the process that owns the
// overlay's swap chain would mean a fault in any of it takes the ESP down with
// it. Out here the worst case is the tab reporting that the helper stopped
// answering.
//
// WHY THIS IS WRITTEN IN OLD C#
//
// It is compiled by the C# compiler that ships with Windows
// (Microsoft.NET\Framework64\v4.0.30319\csc.exe) so that building Doener never
// requires installing the .NET SDK. That compiler predates async Main, ranges,
// using-declarations, target-typed new and nullable annotations, so none of
// them appear here. QuorumAPI targets netstandard, which a .NET Framework exe
// can reference through the netstandard.dll facade.
//
// Protocol - one line in, one reply, connection closes after each:
//
//   STATUS          -> OK attached=0|1
//   ATTACH          -> OK attached | ERR <reason>
//   EXEC <base64>   -> OK | ERR <reason>
//   KILL            -> OK
//   LOG             -> queued output lines, "." on its own line terminates
//
// The script arrives base64-encoded so newlines survive the line protocol.

using System;
using System.Collections.Concurrent;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Threading;
using System.Drawing;
using QuorumAPI;

internal static class Program
{
    private const string PipeName = "doener_exec";

    // Four listeners, not one.
    //
    // AttachAPI and ExecuteScript can take a while, and with a single instance
    // the overlay's STATUS poll had nothing to connect to for the whole
    // duration - it read that as "helper did not answer" and reported the
    // attach as failed even when the attach had actually worked. Several
    // instances means a slow verb never starves the cheap ones.
    private const int Instances = 4;

    // Quorum's own calls are serialised; STATUS and LOG deliberately are not,
    // so state can still be read while an attach is in flight.
    private static readonly object _gate = new object();

    private static QuorumModule _quorum;
    private static readonly ConcurrentQueue<string> _pending = new ConcurrentQueue<string>();

    private static void Main()
    {
        // No message boxes: this process is headless and started by the overlay,
        // so a modal dialog would be an invisible window blocking the pipe.
        try
        {
            QuorumModule.DumbMode = false;
            QuorumModule._AutoUpdateLogs = false;
        }
        catch (Exception ex) { Enqueue("[helper] settings: " + ex.Message); }

        try
        {
            _quorum = new QuorumModule();
            _quorum.AutoUpdate();
        }
        catch (Exception ex)
        {
            Enqueue("[helper] init failed: " + ex.Message);
        }

        // Route Quorum's own output into the queue the overlay drains with LOG.
        try
        {
            QuorumModule.UseOutput(true);
            QuorumModule.Logger.OnLog += OnQuorumLog;
            QuorumModule.Logger.StartRobloxLogWatcher(1000);
        }
        catch (Exception ex)
        {
            Enqueue("[helper] output unavailable: " + ex.Message);
        }

        Enqueue("[helper] ready");

        for (int i = 0; i < Instances; i++)
        {
            Thread t = new Thread(ServeLoop);
            t.IsBackground = true;
            t.Start();
        }
        Thread.Sleep(Timeout.Infinite);
    }

    private static void ServeLoop()
    {
        while (true)
        {
            try
            {
                using (NamedPipeServerStream server = new NamedPipeServerStream(
                           PipeName, PipeDirection.InOut, Instances, PipeTransmissionMode.Byte))
                {
                    server.WaitForConnection();

                    StreamReader reader = new StreamReader(server, Encoding.UTF8);
                    StreamWriter writer = new StreamWriter(server, new UTF8Encoding(false));
                    writer.AutoFlush = true;

                    string line = reader.ReadLine();
                    if (line == null) continue;

                    writer.WriteLine(Handle(line.Trim()));
                    try { server.WaitForPipeDrain(); } catch { }
                }
            }
            catch (Exception ex)
            {
                Enqueue("[helper] " + ex.Message);
                Thread.Sleep(50);
            }
        }
    }

    // Logger.OnLog is Action<string, Color> on this build - the guide's
    // EventHandler shape is from an older API. The colour is Quorum's severity
    // tint; the overlay does its own styling, so only the text is kept.
    private static void OnQuorumLog(string message, Color color)
    {
        Enqueue(message);
    }

    private static void Enqueue(string s)
    {
        if (string.IsNullOrEmpty(s) || s.Trim().Length == 0) return;
        _pending.Enqueue(s);
        string drop;
        while (_pending.Count > 500) _pending.TryDequeue(out drop);
    }

    private static string Handle(string cmd)
    {
        if (string.Equals(cmd, "STATUS", StringComparison.OrdinalIgnoreCase))
            return "OK attached=" + (SafeAttached() ? "1" : "0");

        if (string.Equals(cmd, "ATTACH", StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                lock (_gate)
                {
                    // AttachAPI covers every running client. Attach-by-PID is
                    // documented as unreliable in this build, so it is not
                    // offered. It returns void here, so success is read back
                    // from IsAttached() rather than from a result code.
                    //
                    // The retry is for AutoUpdate: it is started when this
                    // process launches and Quorum refuses to attach while it
                    // runs ("AutoUpdate is still running. Attach blocked."), so
                    // the first attach after a cold start always lost that race
                    // and reported a failure for something that would have
                    // worked a second later.
                    if (!AttachWithRetry(15000))
                        return "ERR attach did not take";
                }
                return "OK attached";
            }
            catch (Exception ex) { return "ERR " + ex.Message; }
        }

        if (cmd.Length > 5 && cmd.Substring(0, 5).ToUpperInvariant() == "EXEC ")
        {
            try
            {
                string script = Encoding.UTF8.GetString(
                    Convert.FromBase64String(cmd.Substring(5).Trim()));

                lock (_gate)
                {
                    if (!SafeAttached() && !AttachWithRetry(15000))
                        return "ERR not attached";
                    _quorum.ExecuteScript(script);
                }
                return "OK";
            }
            catch (FormatException) { return "ERR script was not valid base64"; }
            catch (Exception ex) { return "ERR " + ex.Message; }
        }

        if (string.Equals(cmd, "KILL", StringComparison.OrdinalIgnoreCase))
        {
            try { QuorumModule.KillRoblox(); return "OK"; }
            catch (Exception ex) { return "ERR " + ex.Message; }
        }

        if (string.Equals(cmd, "LOG", StringComparison.OrdinalIgnoreCase))
        {
            StringBuilder sb = new StringBuilder();
            string l;
            while (_pending.TryDequeue(out l)) sb.AppendLine(l);
            sb.Append(".");
            return sb.ToString();
        }

        return "ERR unknown command";
    }

    // Keeps asking until it takes or the budget runs out. Quorum blocks attach
    // while its own AutoUpdate is in flight, and that finishes on its own - so
    // waiting is the correct response, not reporting a failure.
    private static bool AttachWithRetry(int budgetMs)
    {
        System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();
        bool announced = false;
        while (true)
        {
            try { _quorum.AttachAPI(); } catch (Exception ex) { Enqueue("[helper] " + ex.Message); }
            if (SafeAttached())
            {
                Enqueue("[helper] attached after " + sw.ElapsedMilliseconds + "ms");
                return true;
            }
            if (sw.ElapsedMilliseconds >= budgetMs) return false;
            if (!announced)
            {
                Enqueue("[helper] attach blocked, waiting for AutoUpdate...");
                announced = true;
            }
            Thread.Sleep(500);
        }
    }

    private static bool SafeAttached()
    {
        try { return _quorum != null && _quorum.IsAttached(); }
        catch { return false; }
    }
}
