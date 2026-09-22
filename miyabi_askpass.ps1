# Asks the operator for the Miyabi one-time password and hands it straight to ssh.
#
# OpenSSH calls this helper when SSH_ASKPASS points at miyabi_askpass.cmd and
# SSH_ASKPASS_REQUIRE=force, which is what makes an unattended shell (no console)
# able to complete Miyabi's keyboard-interactive step: the answer goes from this
# dialog into ssh's own standard input, and is never written to a file or a log.
# The prompt text ssh passes in (for example "Verification code:") is shown as-is.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Prompt)

Add-Type -AssemblyName Microsoft.VisualBasic
$message = if ($Prompt) { ($Prompt -join " ").Trim() } else { "Miyabi one-time password:" }
$answer = [Microsoft.VisualBasic.Interaction]::InputBox($message, "Miyabi (miyabi-c.jcahpc.jp)", "")
if ([string]::IsNullOrEmpty($answer)) { exit 1 }
[Console]::Out.Write($answer)
exit 0
