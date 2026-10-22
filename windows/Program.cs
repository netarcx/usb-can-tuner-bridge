using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace UsbCanTuner;

internal static class Program
{
    internal static readonly string DataDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UsbCanTuner");

    [STAThread]
    private static void Main(string[] args)
    {
        Directory.CreateDirectory(DataDirectory);
        if (args.Contains("--smoke-test"))
        {
            try
            {
                var payload = Payload.Extract();
                File.WriteAllText(Path.Combine(DataDirectory, "smoke-test.json"),
                    JsonSerializer.Serialize(new { ok = true, payload, runtime = Environment.Version.ToString() }));
            }
            catch (Exception ex)
            {
                File.WriteAllText(Path.Combine(DataDirectory, "smoke-test.json"), JsonSerializer.Serialize(new { ok = false, error = ex.ToString() }));
                Environment.ExitCode = 1;
            }
            return;
        }
        using var mutex = new Mutex(true, "Local\\UsbCanTuner-7FB6B0CB", out bool first);
        if (!first)
        {
            MessageBox.Show("USB-CAN Tuner Bridge is already running. Check its window or system tray.", "USB-CAN Tuner Bridge");
            return;
        }
        ApplicationConfiguration.Initialize();
        Application.ThreadException += (_, e) => MessageBox.Show(e.Exception.Message, "USB-CAN Tuner Bridge");
        Application.Run(new MainForm());
    }
}

internal static class Payload
{
    internal static string Extract()
    {
        var assembly = Assembly.GetExecutingAssembly();
        var entries = assembly.GetManifestResourceNames().Where(n => n.StartsWith("Payload.")).Order().Select(n =>
        {
            using var stream = assembly.GetManifestResourceStream(n)!;
            using var buffer = new MemoryStream();
            stream.CopyTo(buffer);
            return (Name: n[8..], Bytes: buffer.ToArray());
        }).ToArray();
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var entry in entries) { hash.AppendData(Encoding.UTF8.GetBytes(entry.Name)); hash.AppendData(entry.Bytes); }
        var directory = Path.Combine(Program.DataDirectory, "backend", Convert.ToHexString(hash.GetHashAndReset())[..20]);
        Directory.CreateDirectory(directory);
        foreach (var entry in entries)
        {
            var path = Path.Combine(directory, entry.Name);
            if (File.Exists(path) && File.ReadAllBytes(path).AsSpan().SequenceEqual(entry.Bytes)) continue;
            var temp = path + ".tmp";
            File.WriteAllBytes(temp, entry.Bytes);
            File.Move(temp, path, true);
        }
        return directory;
    }
}

internal static class Commands
{
    internal static async Task<string> Run(string program, IEnumerable<string> arguments, int timeoutSeconds = 30)
    {
        var start = new ProcessStartInfo(program) { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, WorkingDirectory = Program.DataDirectory };
        foreach (var arg in arguments) start.ArgumentList.Add(arg);
        using var process = Process.Start(start) ?? throw new IOException($"Could not start {program}.");
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds));
        try { await process.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException)
        {
            // Never kill the process tree: a detached backend may own a firmware session.
            try { process.Kill(entireProcessTree: false); } catch (InvalidOperationException) { }
            throw new IOException($"{program} timed out. A started backend is left running; no USB reset was attempted.");
        }
        var stdout = (await output).Replace("\0", "").Trim();
        var stderr = (await error).Replace("\0", "").Trim();
        if (process.ExitCode != 0) throw new IOException(string.IsNullOrWhiteSpace(stderr) ? stdout : stderr);
        return stdout;
    }

    internal static string Usbipd => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "usbipd-win", "usbipd.exe");
}

internal sealed record Adapter(string BusId, string Serial, string Description, bool Shared, bool Attached)
{
    public override string ToString() => $"{Description}  •  USB {BusId}  •  {Serial}";
}

internal sealed class MainForm : Form
{
    private readonly Label status = new() { Text = "Checking your setup…", AutoSize = true, Font = new Font("Segoe UI", 19, FontStyle.Bold) };
    private readonly Label detail = new() { AutoSize = true, MaximumSize = new Size(780, 0) };
    private readonly Label devices = new() { AutoSize = true, MaximumSize = new Size(780, 0), Text = "Power the CAN devices and connect CAN-H / CAN-L." };
    private readonly ComboBox distro = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 160 };
    private readonly ComboBox profile = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 245 };
    private readonly ComboBox adapters = new() { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Fill };
    private readonly Button retry = new() { Text = "Retry connection", AutoSize = true };
    private readonly Button setup = new() { Text = "First-time setup", AutoSize = true };
    private readonly Button tuner = new() { Text = "Open Phoenix Tuner", AutoSize = true };
    private readonly TextBox log = new() { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, Font = new Font("Consolas", 9) };
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 2500 };
    private readonly HttpClient http = new(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(2) };
    private readonly NotifyIcon tray;
    private string payload = "", linuxPayload = "", preparedKey = "", uid = "", lastMessage = "";
    private string currentInterface = "can0";
    private bool busy, faulted, sessionObserved, permitExit, keepAliveStarted;
    private string? managedSerial;
    private int readFailures;
    private int pollCount;
    private string Profile => profile.SelectedIndex == 1 ? "alpha" : "stable";
    private string Distro => distro.SelectedItem?.ToString() ?? "Ubuntu";

    internal MainForm()
    {
        Text = "USB-CAN Tuner Bridge";
        Font = new Font("Segoe UI", 10);
        ClientSize = new Size(850, 640);
        MinimumSize = new Size(780, 660);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(247, 249, 252);
        Icon = SystemIcons.Application;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(24), ColumnCount = 1, RowCount = 11 };
        for (var row = 0; row < 10; row++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        Controls.Add(layout);
        layout.Controls.Add(new Label { Text = "USB-CAN  →  PHOENIX TUNER", ForeColor = Color.FromArgb(34, 94, 176), AutoSize = true, Font = new Font(Font, FontStyle.Bold), Margin = new Padding(0, 0, 0, 18) });
        layout.Controls.Add(status);
        detail.Margin = new Padding(0, 8, 0, 18); layout.Controls.Add(detail);
        var settings = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, WrapContents = false, Margin = new Padding(0, 0, 0, 14) };
        profile.Items.AddRange(["2026 stable • firmware 26.3.x", "2027 alpha • firmware 26.70.x"]); profile.SelectedIndex = 0;
        settings.Controls.Add(new Label { Text = "Linux", AutoSize = true, Padding = new Padding(0, 6, 0, 0) }); settings.Controls.Add(distro);
        settings.Controls.Add(profile); settings.Controls.Add(setup); layout.Controls.Add(settings);
        layout.Controls.Add(adapters);
        var target = new Label { AutoSize = true, Text = "Tuner target: localhost   •   Port: 1250   •   CANivore USB: OFF", Font = new Font(Font, FontStyle.Bold), Margin = new Padding(0, 18, 0, 14) };
        layout.Controls.Add(target);
        layout.Controls.Add(devices);
        var buttons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 14, 0, 14) };
        buttons.Controls.Add(tuner); buttons.Controls.Add(retry);
        var copy = new Button { Text = "Copy target", AutoSize = true }; copy.Click += (_, _) => Clipboard.SetText("localhost"); buttons.Controls.Add(copy);
        var folder = new Button { Text = "Open logs", AutoSize = true }; folder.Click += (_, _) => Process.Start(new ProcessStartInfo("explorer.exe", Program.DataDirectory) { UseShellExecute = true }); buttons.Controls.Add(folder);
        layout.Controls.Add(buttons);
        layout.Controls.Add(new Label { AutoSize = true, MaximumSize = new Size(780, 0), Text = "The bridge stays running when this window closes. It never starts a firmware update, feeds robot enable, or automatically resets an active adapter.", ForeColor = Color.DimGray, Margin = new Padding(0, 0, 0, 12) });
        layout.Controls.Add(new Label { AutoSize = true, Text = "Connection log", Margin = new Padding(0, 0, 0, 5) });
        layout.Controls.Add(log);
        var menu = new ContextMenuStrip();
        menu.Items.Add("Show bridge", null, (_, _) => RestoreWindow());
        menu.Items.Add("Exit launcher (leave bridge running)", null, (_, _) => { permitExit = true; Close(); });
        tray = new NotifyIcon { Text = "USB-CAN Tuner Bridge", Icon = SystemIcons.Application, Visible = true, ContextMenuStrip = menu };
        tray.DoubleClick += (_, _) => RestoreWindow();
        FormClosing += (_, e) => { if (!permitExit && e.CloseReason is CloseReason.UserClosing or CloseReason.TaskManagerClosing) { e.Cancel = true; Hide(); tray.ShowBalloonTip(3000, "USB-CAN bridge stays connected", "Double-click this tray icon to reopen. Closing the launcher does not stop the backend.", ToolTipIcon.Info); } };
        FormClosed += (_, _) => { timer.Stop(); tray.Dispose(); http.Dispose(); };
        retry.Click += async (_, _) => { if (busy) return; faulted = false; readFailures = 0; preparedKey = ""; await Tick(); };
        setup.Click += (_, _) => LaunchSetup();
        tuner.Click += async (_, _) => await OpenTuner();
        distro.SelectedIndexChanged += (_, _) => { linuxPayload = ""; preparedKey = ""; };
        profile.SelectedIndexChanged += (_, _) => { preparedKey = ""; };
        timer.Tick += async (_, _) => await Tick();
        Shown += async (_, _) =>
        {
            try
            {
                payload = Payload.Extract();
                var names = await Commands.Run("wsl.exe", ["--list", "--quiet"], 15);
                foreach (var name in names.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries).Select(n => n.Trim()))
                    if (Regex.IsMatch(name, @"^[\w.\-]+$")) distro.Items.Add(name);
                if (distro.Items.Count == 0) throw new IOException("Install WSL2 and Ubuntu with First-time setup, then launch Ubuntu once to create your Linux user.");
                distro.SelectedIndex = Math.Max(0, distro.Items.IndexOf("Ubuntu"));
                Log("Launcher started. Firmware programming remains entirely in Phoenix Tuner.");
                timer.Start(); await Tick();
            }
            catch (Exception ex) { Fail(ex); }
        };
    }

    private void RestoreWindow() { Show(); WindowState = FormWindowState.Normal; Activate(); }
    private void Log(string text)
    {
        if (lastMessage == text) return;
        lastMessage = text;
        var line = $"{DateTime.Now:HH:mm:ss}  {text}{Environment.NewLine}";
        log.AppendText(line);
        try { File.AppendAllText(Path.Combine(Program.DataDirectory, "launcher.log"), line); } catch (IOException) { }
    }
    private void State(string heading, string message, Color color)
    { status.Text = heading; status.ForeColor = color; detail.Text = message; }
    private void Fail(Exception ex)
    {
        faulted = true;
        State("Connection needs attention", ex.Message, Color.FromArgb(161, 75, 20));
        Log(ex.Message);
        if (!sessionObserved) { distro.Enabled = true; profile.Enabled = true; }
    }

    private static string Field(JsonElement value, string name) =>
        value.TryGetProperty(name, out var field) ? field.ToString() : "unknown";

    private static bool TunerIsOpen()
    {
        var running = false;
        foreach (var process in Process.GetProcesses())
        {
            using (process)
            {
                try { if (process.ProcessName.Contains("phoenix", StringComparison.OrdinalIgnoreCase) && process.ProcessName.Contains("tuner", StringComparison.OrdinalIgnoreCase)) running = true; }
                catch (InvalidOperationException) { }
            }
        }
        return running;
    }

    private async Task<JsonElement?> ReadDiagnostic(string action)
    {
        try
        {
            var text = await http.GetStringAsync($"http://127.0.0.1:1250/?action={action}&canbus={Uri.EscapeDataString(currentInterface)}");
            using var document = JsonDocument.Parse(text);
            if (!document.RootElement.TryGetProperty("GeneralReturn", out var general) || general.GetProperty("Error").GetInt32() != 0) return null;
            return document.RootElement.Clone();
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException) { return null; }
    }

    private async Task<string> Helper(string action, string serial = "")
    {
        if (string.IsNullOrEmpty(linuxPayload))
        {
            uid = await Commands.Run("wsl.exe", ["--distribution", Distro, "--exec", "id", "-u"]);
            if (!int.TryParse(uid, out var value) || value <= 0) throw new IOException("Set a normal default Linux user in this WSL distribution; the Phoenix server does not run as root.");
            linuxPayload = await Commands.Run("wsl.exe", ["--distribution", Distro, "--exec", "wslpath", "-u", payload]);
        }
        return await Commands.Run("wsl.exe", ["--distribution", Distro, "--user", "root", "--exec", "python3",
            linuxPayload + "/control.py", action, "--uid", uid, "--profile", Profile, "--serial", serial], action == "prepare" ? 240 : 35);
    }

    private void KeepWslSession()
    {
        // This Windows child is independent of the GUI's lifetime. No redirected
        // pipes or process-tree termination tie it to closing this window.
        var start = new ProcessStartInfo("wsl.exe") { UseShellExecute = false,
            CreateNoWindow = true, WorkingDirectory = Program.DataDirectory };
        foreach (var arg in new[] { "--distribution", Distro, "--user", "root", "--exec", "python3",
            linuxPayload + "/control.py", "watch", "--uid", uid }) start.ArgumentList.Add(arg);
        using var watcher = Process.Start(start) ?? throw new IOException("Could not hold the backend's WSL session open.");
    }

    private async Task<Adapter[]> ReadAdapters()
    {
        if (!File.Exists(Commands.Usbipd)) throw new IOException("usbipd-win is missing. Use First-time setup, then retry.");
        using var json = JsonDocument.Parse(await Commands.Run(Commands.Usbipd, ["state"]));
        return json.RootElement.GetProperty("Devices").EnumerateArray().Where(d =>
            d.GetProperty("BusId").ValueKind == JsonValueKind.String &&
            (d.GetProperty("InstanceId").GetString() ?? "").Contains("VID_1D50&PID_606F", StringComparison.OrdinalIgnoreCase)).Select(d =>
        {
            var instance = d.GetProperty("InstanceId").GetString()!;
            return new Adapter(d.GetProperty("BusId").GetString()!, instance.Split('\\')[^1],
                d.GetProperty("Description").GetString() ?? "USB-CAN", d.GetProperty("PersistedGuid").ValueKind == JsonValueKind.String,
                d.GetProperty("ClientIPAddress").ValueKind == JsonValueKind.String);
        }).ToArray();
    }

    private void ShowAdapters(Adapter[] candidates)
    {
        var selection = managedSerial ?? (adapters.SelectedItem as Adapter)?.Serial;
        if (!adapters.Items.Cast<Adapter>().SequenceEqual(candidates))
        {
            adapters.Items.Clear(); adapters.Items.AddRange(candidates);
            if (selection != null) adapters.SelectedIndex = Array.FindIndex(candidates, a => a.Serial == selection);
            else if (candidates.Length == 1) adapters.SelectedIndex = 0;
        }
        adapters.Enabled = managedSerial == null;
    }

    private async Task AttachAdapter(Adapter adapter, bool initialConnection)
    {
        if (!Regex.IsMatch(adapter.BusId, @"^\d+-\d+$") || !Regex.IsMatch(adapter.Serial, @"^[\w.\-]+$"))
            throw new IOException("This adapter's USB identity cannot be mapped safely.");
        if (!adapter.Shared)
        {
            if (!initialConnection) throw new IOException("USB sharing for the original adapter was removed. Re-enable sharing before retrying.");
            Log("Requesting Administrator approval to share USB " + adapter.BusId + " once.");
            var start = new ProcessStartInfo(Commands.Usbipd) { UseShellExecute = true, Verb = "runas", Arguments = "bind --busid " + adapter.BusId };
            using var bind = Process.Start(start) ?? throw new IOException("USB sharing was cancelled.");
            await bind.WaitForExitAsync();
            if (bind.ExitCode != 0) throw new IOException("usbipd could not share this adapter. No driver replacement was forced.");
        }
        await Commands.Run("wsl.exe", ["--distribution", Distro, "--user", "root", "--exec", "/sbin/modprobe", "gs_usb"]);
        if (!adapter.Attached) await Commands.Run(Commands.Usbipd, ["attach", "--wsl", Distro, "--busid", adapter.BusId]);
    }

    private async Task ManagedConnection(JsonElement snapshot, JsonElement? version)
    {
        var record = snapshot.GetProperty("record");
        managedSerial = record.GetProperty("serial").GetString()!;
        currentInterface = record.GetProperty("interface").GetString()!;
        profile.SelectedIndex = record.GetProperty("profile").GetString() == "alpha" ? 1 : 0;
        distro.Enabled = false; profile.Enabled = false;
        sessionObserved = true;
        if (!keepAliveStarted) { KeepWslSession(); keepAliveStarted = true; }
        var candidates = await ReadAdapters();
        ShowAdapters(candidates);
        var adapter = candidates.SingleOrDefault(a => a.Serial.Equals(managedSerial, StringComparison.OrdinalIgnoreCase));
        var connection = snapshot.GetProperty("connection");
        var links = connection.GetProperty("interfaces").EnumerateArray().ToArray();
        var ready = connection.GetProperty("ready").GetBoolean();
        var originalIndex = record.TryGetProperty("ifindex", out var index) ? index.GetInt32() : (int?)null;
        var replacementDown = links.Length == 1 && originalIndex.HasValue &&
            links[0].GetProperty("ifindex").GetInt32() != originalIndex.Value &&
            !links[0].GetProperty("flags").EnumerateArray().Any(f => f.GetString() == "UP");
        if (!ready && !faulted && ((links.Length == 0 && adapter is { Attached: false }) || replacementDown))
        {
            State("Reconnecting your USB-CAN adapter…", "Restoring the original adapter connection. The diagnostic backend stays running.", Color.FromArgb(34, 94, 176));
            devices.Text = "Waiting for CAN devices to reconnect.";
            if (links.Length == 0) await AttachAdapter(adapter!, initialConnection: false);
            using var recovered = JsonDocument.Parse(await Helper("recover", managedSerial));
            connection = recovered.RootElement.GetProperty("connection").Clone();
            ready = connection.GetProperty("ready").GetBoolean();
            Log("Restored USB-CAN attachment without restarting Phoenix or resetting an active CAN interface.");
        }
        else if (ready && (originalIndex == null || links[0].GetProperty("ifindex").GetInt32() != originalIndex))
        {
            // Migrate a healthy v0.1.0 record, or record a link already brought up
            // externally. recover validates identity and never resets an UP link.
            await Helper("recover", managedSerial);
        }
        if (!ready)
        {
            State(links.Length == 0 ? "USB-CAN adapter disconnected" : "CAN connection needs attention",
                connection.GetProperty("reason").GetString() ?? "CAN interface is unavailable.", Color.FromArgb(161, 75, 20));
            devices.Text = "CAN devices are unavailable. The diagnostic server alone does not establish a CAN connection.";
            return;
        }
        if (!version.HasValue)
        {
            State("Diagnostic server is not responding", "The CAN interface is present. The backend was left running; no automatic restart was attempted.", Color.FromArgb(161, 75, 20));
            return;
        }
        if (connection.GetProperty("can_state").GetString() == "ERROR-PASSIVE")
        {
            State("CAN bus reports errors", "Check device power, wiring, and termination. The adapter will not be reset automatically.", Color.FromArgb(161, 75, 20));
            devices.Text = "A working device connection has not been verified.";
            return;
        }
        readFailures = 0;
        var text = version.Value.GetProperty("Version").GetString() ?? "Unknown";
        State("USB-CAN connected", $"Phoenix {text} • {currentInterface}. Open Tuner with the target below.", Color.FromArgb(22, 118, 75));
        if (++pollCount % 2 != 0) return;
        if (TunerIsOpen())
        {
            if (!devices.Text.Contains("firmware ")) devices.Text = "Phoenix Tuner is managing devices. Use its device list while it is open.";
            return;
        }
        var result = await ReadDiagnostic("getdevices");
        if (result.HasValue && result.Value.TryGetProperty("DeviceArray", out var list))
        {
            var rows = list.EnumerateArray().ToArray();
            devices.Text = rows.Length == 0 ? "Waiting for CAN devices. Check power, wiring, and termination." : string.Join(Environment.NewLine,
                rows.Select(d => $"{Field(d, "Name")}  •  ID {Field(d, "ID")}  •  {Field(d, "Model")}  •  firmware {Field(d, "CurrentVers")}"));
            var mismatch = rows.Any(d => d.TryGetProperty("Compliancy", out var c) && c.GetInt32() != version.Value.GetProperty("Compliancy").GetInt32());
            if (mismatch) detail.Text += " Firmware and backend generations differ; self-test may be unavailable. Do not change backends during an update.";
        }
    }

    private async Task Tick()
    {
        if (busy || IsDisposed || distro.SelectedItem == null) return;
        busy = true; retry.Enabled = false;
        try
        {
            var version = await ReadDiagnostic("getversion");
            using var statusDocument = JsonDocument.Parse(await Helper("status"));
            var snapshot = statusDocument.RootElement;
            if (snapshot.GetProperty("running").GetBoolean())
            {
                await ManagedConnection(snapshot, version);
                return;
            }
            if (version.HasValue)
            {
                State("External diagnostic server is available", "USB-CAN connectivity has not been verified for this server. Its active session was left unchanged.", Color.FromArgb(34, 94, 176));
                return;
            }
            if (sessionObserved)
            {
                if (++readFailures >= 3) State("Bridge connection interrupted", "The backend stopped. No automatic restart was attempted; finish any Tuner operation before restarting it.", Color.FromArgb(161, 75, 20));
                return;
            }
            if (faulted) return;
            var candidates = await ReadAdapters();
            ShowAdapters(candidates);
            var key = Distro + ":" + Profile;
            if (preparedKey != key)
            {
                distro.Enabled = false; profile.Enabled = false;
                State("Preparing the Linux backend…", "First launch downloads a pinned CTRE package. Your CAN devices are not changed.", Color.FromArgb(34, 94, 176));
                Log("Preparing " + key);
                await Helper("prepare"); preparedKey = key;
                distro.Enabled = true; profile.Enabled = true;
            }
            if (candidates.Length == 0)
            {
                State("Ready for your USB-CAN adapter", "Plug in a candleLight / CANable adapter (1d50:606f). It will connect automatically.", Color.FromArgb(34, 94, 176));
                return;
            }
            if (adapters.SelectedItem is not Adapter adapter)
            {
                State("Choose an adapter", "Several compatible USB adapters are connected. Select the one wired to your CTRE devices.", Color.FromArgb(34, 94, 176)); return;
            }
            distro.Enabled = false; profile.Enabled = false;
            State("Connecting your USB-CAN adapter…", "Attaching USB to WSL and checking classic CAN at 1 Mbps.", Color.FromArgb(34, 94, 176));
            await AttachAdapter(adapter, initialConnection: true);
            using var started = JsonDocument.Parse(await Helper("start", adapter.Serial));
            currentInterface = started.RootElement.GetProperty("record").GetProperty("interface").GetString()!;
            KeepWslSession(); keepAliveStarted = true;
            sessionObserved = true;
            Log("Backend started on " + currentInterface + ". Closing this launcher leaves it running.");
        }
        catch (Exception ex) { Fail(ex); }
        finally { busy = false; retry.Enabled = true; }
    }

    private void LaunchSetup()
    {
        if (MessageBox.Show("Setup opens an Administrator console to install usbipd-win and the WSL Ubuntu prerequisites. A new WSL installation may require a Windows restart and an initial Linux username. It does not modify CTRE devices.\n\nContinue?", Text, MessageBoxButtons.OKCancel, MessageBoxIcon.Information) != DialogResult.OK) return;
        try
        {
            if (sessionObserved) { MessageBox.Show("A Phoenix backend is active. Finish any firmware update and close Tuner before changing the WSL setup.", Text); return; }
            if (payload.Length == 0) payload = Payload.Extract();
            if (!Regex.IsMatch(Distro, @"^[\w.\-]+$")) throw new IOException("Invalid distribution name.");
            Process.Start(new ProcessStartInfo("powershell.exe") { UseShellExecute = true, Verb = "runas",
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(payload, "setup.ps1") + "\" -Distro " + Distro });
        }
        catch (Exception ex) { Fail(ex); }
    }

    private async Task OpenTuner()
    {
        try
        {
            var result = await Commands.Run("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(payload, "open-tuner.ps1"), "-Profile", Profile]);
            Log(result);
        }
        catch (Exception ex) { MessageBox.Show(ex.Message, Text); }
    }
}
