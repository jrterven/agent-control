import AppKit
import Foundation

guard CommandLine.arguments.count == 4 else { exit(2) }
guard let reference = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let implementation = NSImage(contentsOfFile: CommandLine.arguments[2]) else { exit(3) }

let itemSize = NSSize(width: 424, height: 612)
let gutter: CGFloat = 16
let canvasSize = NSSize(width: itemSize.width * 2 + gutter, height: itemSize.height)
let canvas = NSImage(size: canvasSize)
canvas.lockFocus()
NSColor(calibratedWhite: 0.035, alpha: 1).setFill()
NSRect(origin: .zero, size: canvasSize).fill()
reference.draw(in: NSRect(x: 0, y: 0, width: itemSize.width, height: itemSize.height))
implementation.draw(in: NSRect(x: itemSize.width + gutter, y: 0, width: itemSize.width, height: itemSize.height))
canvas.unlockFocus()

guard let tiff = canvas.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else { exit(4) }
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[3]), options: .atomic)
