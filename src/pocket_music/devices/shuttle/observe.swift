import Foundation
import AppKit
import Vision
import CoreGraphics
import ImageIO
let started=DispatchTime.now().uptimeNanoseconds
func finish(_ value:[String:Any]) -> Never {
 var v=value;v["started_ns"]=started;v["completed_ns"]=DispatchTime.now().uptimeNanoseconds
 let data=try! JSONSerialization.data(withJSONObject:v,options:[.sortedKeys]);print(String(data:data,encoding:.utf8)!);exit(0)
}
guard let app=NSRunningApplication.runningApplications(withBundleIdentifier:"com.serato.seratodj").first, let url=app.bundleURL else {finish(["status":"serato_not_running"])}
let version=Bundle(url:url)?.object(forInfoDictionaryKey:"CFBundleShortVersionString") as? String ?? ""
guard version.hasPrefix("5.0.0") else {finish(["status":"unsupported_app_version","version":version])}
let windows=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
let candidates=windows.filter{ ($0[kCGWindowOwnerName as String] as? String)=="Serato DJ Pro" && ($0[kCGWindowLayer as String] as? Int)==0 }
let main=candidates.max{ a,b in
 let x=a[kCGWindowBounds as String] as? [String:Double] ?? [:], y=b[kCGWindowBounds as String] as? [String:Double] ?? [:]
 return (x["Width"] ?? 0)*(x["Height"] ?? 0)<(y["Width"] ?? 0)*(y["Height"] ?? 0)
}
guard let w=main, let id=w[kCGWindowNumber as String] as? Int, let bounds=w[kCGWindowBounds as String] as? [String:Double], bounds["Width"]==1470,bounds["Height"]==956 else {finish(["status":"unsupported_window","windows":candidates.map{["id":$0[kCGWindowNumber as String] ?? 0,"bounds":$0[kCGWindowBounds as String] ?? [:]]}])}
let path=CommandLine.arguments[1]
let capture=Process();capture.executableURL=URL(fileURLWithPath:"/usr/sbin/screencapture");capture.arguments=["-x","-o","-l\(id)",path]
try capture.run();capture.waitUntilExit()
guard capture.terminationStatus==0,let src=CGImageSourceCreateWithURL(URL(fileURLWithPath:path) as CFURL,nil),let image=CGImageSourceCreateImageAtIndex(src,0,nil) else {finish(["status":"capture_failed"])}
let factor=Double(image.width)/1470
let crop=image.cropping(to:CGRect(x:0,y:0,width:image.width,height:Int(225*factor)))!
let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate;request.usesLanguageCorrection=false
request.usesCPUOnly=true
try VNImageRequestHandler(cgImage:crop,options:[:]).perform([request])
let texts=(request.results ?? []).compactMap{ o -> [String:Any]? in
 guard let t=o.topCandidates(1).first else{return nil}
 return ["text":t.string,"confidence":t.confidence,"x":Double(o.boundingBox.midX)*1470,"y":(1-Double(o.boundingBox.midY))*225]
}
let bytes=UnsafeMutablePointer<UInt8>.allocate(capacity:image.width*image.height*4);defer{bytes.deallocate()}
let context=CGContext(data:bytes,width:image.width,height:image.height,bitsPerComponent:8,bytesPerRow:image.width*4,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)!
context.draw(image,in:CGRect(x:0,y:0,width:image.width,height:image.height))
func pixel(_ x:Double,_ y:Double)->[Int] {let offset=(Int(y*factor)*image.width+Int(x*factor))*4;return [Int(bytes[offset]),Int(bytes[offset+1]),Int(bytes[offset+2])]}
func blue(_ rgb:[Int])->Bool {rgb[2]>90 && rgb[1]>55 && rgb[0]<80}
let left=pixel(499,139),right=pixel(1098,139),midi=pixel(239,45)
let extended=texts.contains{($0["text"] as? String)=="Extended"}
let supported=extended && !blue(midi)
finish(["status":supported ? "ok":"unsupported_view","profile":"serato-5-extended-1470x956/v1","window_id":id,"serato_pid":app.processIdentifier,"app_version":version,"texts":texts,"left_playing":supported ? blue(left):NSNull(),"right_playing":supported ? blue(right):NSNull(),"pixels":["left_play":left,"right_play":right,"midi":midi],"image_path":path])
