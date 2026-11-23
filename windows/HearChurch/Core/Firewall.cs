using System.ComponentModel;
using System.Diagnostics;

namespace HearChurch.Core;

/// <summary>"This Wi-Fi only": phones connect straight to the engine, which then listens on the network (0.0.0.0, on
/// the port the app picked at launch). Windows Firewall blocks that unless a rule lets it in. The port changes every
/// launch, so the rule is for the engine's program instead: inbound TCP to hearchurch-engine.exe, every network type
/// (a church Wi-Fi is usually "Public" on Windows). Added once, the first time the operator picks "This Wi-Fi only",
/// with netsh run elevated: Windows asks for permission (UAC) once. docs/windows-app.md.</summary>
public static class Firewall
{
    public const string RuleName = "Hear Church (This Wi-Fi only)";
    /// <summary>Live: phones on the Wi-Fi send UDP to the engine (live/protocol.py, port 47300) and find it by Bonjour (5353).
    /// The same rule lets them reach the engine's listening page (TCP) while the server is lost mid-meeting
    /// (docs/rooms.md): so it is for any protocol, still only inbound to the engine. Renamed from "Hear Church (Live)",
    /// a UDP-only rule, so PCs that have that one are asked once more.</summary>
    public const string LiveRuleName = "Hear Church (Live and Wi-Fi)";

    /// <summary>Is there already a rule for this engine (this install's path)? netsh's "show rule" needs no elevation.</summary>
    public static bool HasRule(string exe, string rule = RuleName)
    {
        try
        {
            var psi = new ProcessStartInfo("netsh.exe")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var a in new[] { "advfirewall", "firewall", "show", "rule", $"name={rule}", "verbose" }) psi.ArgumentList.Add(a);
            using var p = Process.Start(psi)!;
            var output = p.StandardOutput.ReadToEnd();
            p.WaitForExit(15000);
            return p.ExitCode == 0 && output.Contains(exe, StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    /// <summary>The netsh arguments that add the rule (one string: the elevated start can't take an argument list).</summary>
    public static string AddRuleArguments(string exe, bool live = false) => live
        ? $"advfirewall firewall add rule name=\"{LiveRuleName}\" dir=in action=allow protocol=any profile=any " +
          $"enable=yes program=\"{exe}\" description=\"Phones on this Wi-Fi hear the room audio (Live), and keep listening if the server is lost.\""
        : $"advfirewall firewall add rule name=\"{RuleName}\" dir=in action=allow protocol=TCP profile=any enable=yes " +
        $"program=\"{exe}\" description=\"Phones on this Wi-Fi reach Hear Church's translation (Settings: This Wi-Fi only).\"";

    /// <summary>Makes sure the rule is there; asks Windows (UAC) only if it isn't. Null when phones can connect, or what
    /// to tell the operator.</summary>
    public static async Task<string?> Allow(bool live = false)
    {
        if (Engine.EnginePath() is not { } exe) return null;   // a development build without the sidecar
        exe = Path.GetFullPath(exe);
        return await Task.Run(() =>
        {
            if (HasRule(exe, live ? LiveRuleName : RuleName)) return null;
            try
            {
                using var p = Process.Start(new ProcessStartInfo("netsh.exe", AddRuleArguments(exe, live))
                {
                    UseShellExecute = true,
                    Verb = "runas",
                    WindowStyle = ProcessWindowStyle.Hidden,
                })!;
                p.WaitForExit(60000);
                if (p.ExitCode == 0) return null;
            }
            catch (Win32Exception e) when (e.NativeErrorCode == 1223)   // the operator said No to Windows' question
            {
                return live ? LiveDeclined : Declined;
            }
            catch { }
            return HasRule(exe, live ? LiveRuleName : RuleName) ? null : live ? LiveDeclined : Declined;
        });
    }

    public const string LiveDeclined = "Windows Firewall may keep phones on this Wi-Fi from hearing Live. Choose Allow in Windows Firewall below, then Yes when Windows asks.";
    public const string Declined = "Windows Firewall may keep phones on this Wi-Fi from connecting to this PC. Choose This Wi-Fi only again and allow Hear Church when Windows asks.";
}
