import AppKit
import Foundation

guard CommandLine.arguments.count == 4 else {
  fputs("usage: combine.swift reference.png implementation.png output.png\n", stderr)
  exit(2)
}

let referencePath = CommandLine.arguments[1]
let implementationPath = CommandLine.arguments[2]
let outputPath = CommandLine.arguments[3]

guard let reference = NSImage(contentsOfFile: referencePath),
      let implementation = NSImage(contentsOfFile: implementationPath) else {
  fputs("unable to open input image\n", stderr)
  exit(3)
}

let itemSize = NSSize(width: 390, height: 844)
let gutter: CGFloat = 20
let canvas = NSImage(size: NSSize(width: itemSize.width * 2 + gutter, height: itemSize.height))
canvas.lockFocus()
NSColor(calibratedWhite: 0.08, alpha: 1).setFill()
NSRect(origin: .zero, size: canvas.size).fill()
reference.draw(in: NSRect(x: 0, y: 0, width: itemSize.width, height: itemSize.height), from: .zero, operation: .copy, fraction: 1)
implementation.draw(in: NSRect(x: itemSize.width + gutter, y: 0, width: itemSize.width, height: itemSize.height), from: .zero, operation: .copy, fraction: 1)
canvas.unlockFocus()

guard let tiff = canvas.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else {
  fputs("unable to encode output image\n", stderr)
  exit(4)
}

try png.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
