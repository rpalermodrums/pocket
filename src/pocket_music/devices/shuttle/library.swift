import AppKit
import CoreGraphics
import Foundation
let open=Process();open.executableURL=URL(fileURLWithPath:"/usr/bin/open");open.arguments=["-a","Serato DJ Pro"];try open.run();open.waitUntilExit();Thread.sleep(forTimeInterval:0.3)
if CommandLine.arguments.count==2 && CommandLine.arguments[1]=="--activate" { exit(NSWorkspace.shared.frontmostApplication?.bundleIdentifier=="com.serato.seratodj" ? 0:2) }
guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier=="com.serato.seratodj", let count=Int(CommandLine.arguments[1]),let index=Int(CommandLine.arguments[2]), (1...64).contains(count),(0..<count).contains(index) else{exit(2)}
// Transport and loop controls can move focus to the crate sidebar. Focus the
// qualified track table before sending navigation keys, relative to its window.
let windows=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
let eligible=windows.filter{($0[kCGWindowOwnerName as String] as? String)=="Serato DJ Pro" && ($0[kCGWindowLayer as String] as? Int)==0}
guard let window=eligible.first(where:{
 let b=$0[kCGWindowBounds as String] as? [String:Double] ?? [:]
 return b["Width"]==1470 && b["Height"]==956
}),let bounds=window[kCGWindowBounds as String] as? [String:Double] else {exit(5)}
if CommandLine.arguments.count>3 && FileManager.default.fileExists(atPath:CommandLine.arguments[3]) {exit(3)}
let point=CGPoint(x:(bounds["X"] ?? 0)+450,y:(bounds["Y"] ?? 0)+510)
CGEvent(mouseEventSource:nil,mouseType:.leftMouseDown,mouseCursorPosition:point,mouseButton:.left)?.post(tap:.cghidEventTap)
CGEvent(mouseEventSource:nil,mouseType:.leftMouseUp,mouseCursorPosition:point,mouseButton:.left)?.post(tap:.cghidEventTap)
Thread.sleep(forTimeInterval:0.1)
func key(_ code:CGKeyCode) {
 if CommandLine.arguments.count>3 && FileManager.default.fileExists(atPath:CommandLine.arguments[3]) {exit(3)}
 guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier=="com.serato.seratodj" else {exit(4)}
 let d=CGEvent(keyboardEventSource:nil,virtualKey:code,keyDown:true)!;d.flags=[];d.post(tap:.cghidEventTap);Thread.sleep(forTimeInterval:0.015)
 let u=CGEvent(keyboardEventSource:nil,virtualKey:code,keyDown:false)!;u.flags=[];u.post(tap:.cghidEventTap);Thread.sleep(forTimeInterval:0.035)
}
for _ in 0...count {key(126)}
for _ in 0..<index {key(125)}
Thread.sleep(forTimeInterval:0.15)
