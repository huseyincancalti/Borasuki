param([Parameter(Mandatory=$true)][string]$Payload)
$ErrorActionPreference = 'Stop'
$message = Get-Content -LiteralPath $Payload -Raw -Encoding UTF8 | ConvertFrom-Json
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$null = $texts.Item(0).AppendChild($xml.CreateTextNode([string]$message.title))
$null = $texts.Item(1).AppendChild($xml.CreateTextNode([string]$message.body))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag = 'completed'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Borasuki').Show($toast)
