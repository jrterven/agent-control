import AppKit
import Foundation

guard CommandLine.arguments.count == 4 else { exit(2) }
guard let reference = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let implementation = NSImage(contentsOfFile: CommandLine.arguments[2]) else { exit(3) }

let targetHeight: CGFloat = 651
let gutter: CGFloat = 20
let labelHeight: CGFloat = 38

func fittedWidth(for image: NSImage) -> CGFloat {
    targetHeight * image.size.width / image.size.height
}

let referenceWidth = fittedWidth(for: reference)
let implementationWidth = fittedWidth(for: implementation)
let canvasSize = NSSize(
    width: referenceWidth + implementationWidth + gutter,
    height: targetHeight + labelHeight
)
let canvas = NSImage(size: canvasSize)
canvas.lockFocus()

NSColor(calibratedWhite: 0.035, alpha: 1).setFill()
NSRect(origin: .zero, size: canvasSize).fill()

let labelAttributes: [NSAttributedString.Key: Any] = [
    .font: NSFont.systemFont(ofSize: 15, weight: .semibold),
    .foregroundColor: NSColor(calibratedWhite: 0.84, alpha: 1),
]

NSAttributedString(string: "Referencia", attributes: labelAttributes)
    .draw(at: NSPoint(x: 12, y: targetHeight + 10))
NSAttributedString(string: "Implementacion", attributes: labelAttributes)
    .draw(at: NSPoint(x: referenceWidth + gutter + 12, y: targetHeight + 10))

reference.draw(in: NSRect(x: 0, y: 0, width: referenceWidth, height: targetHeight))
implementation.draw(
    in: NSRect(
        x: referenceWidth + gutter,
        y: 0,
        width: implementationWidth,
        height: targetHeight
    )
)

canvas.unlockFocus()

guard let tiff = canvas.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else { exit(4) }
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[3]), options: .atomic)
