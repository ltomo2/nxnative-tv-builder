import AppKit
let source = NSImage(contentsOfFile: CommandLine.arguments[1])!
let output = CommandLine.arguments[2]
let w = Int(CommandLine.arguments[3])!, h = Int(CommandLine.arguments[4])!
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSGraphicsContext.current!.imageInterpolation = .high
let bounds = NSRect(x: 0, y: 0, width: w, height: h)
let gradient = NSGradient(starting: NSColor(srgbRed: 0.02, green: 0.08, blue: 0.17, alpha: 1), ending: NSColor(srgbRed: 0.10, green: 0.02, blue: 0.22, alpha: 1))!
let mode = CommandLine.arguments[5]
if mode == "back" { gradient.draw(in: bounds, angle: 0) } else { NSGraphicsContext.current!.cgContext.clear(bounds) }
let side = Double(h) * 0.94
if mode == "front" { source.draw(in: NSRect(x: (Double(w)-side)/2, y: (Double(h)-side)/2, width: side, height: side), from: .zero, operation: .sourceOver, fraction: 1) }
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output))
