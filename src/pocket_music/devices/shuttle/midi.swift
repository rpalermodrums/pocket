import Foundation
import CoreMIDI

let outputLock = NSLock()
func emit(_ obj: [String: Any]) {
 outputLock.lock(); defer { outputLock.unlock() }
 var value = obj
 value["monotonic_ns"] = DispatchTime.now().uptimeNanoseconds
 if let data = try? JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]), let line=String(data:data,encoding:.utf8) {
  print(line); fflush(stdout)
 }
}
func name(_ object:MIDIObjectRef)->String {
 var value:Unmanaged<CFString>?
 if MIDIObjectGetStringProperty(object,kMIDIPropertyDisplayName,&value)==noErr, let s=value { return s.takeRetainedValue() as String }
 return "unknown"
}
var client=MIDIClientRef(), source=MIDIEndpointRef(), destination=MIDIEndpointRef(), input=MIDIPortRef()
func checked(_ status:OSStatus,_ operation:String) {
 if status != noErr { emit(["event":"error","operation":operation,"status":status]); exit(2) }
}
func packets(_ list:UnsafePointer<MIDIPacketList>,_ origin:String) {
 withUnsafePointer(to:list.pointee.packet) { first in
  var ptr=first
  for _ in 0..<list.pointee.numPackets {
   let packet=ptr.pointee
   let data=withUnsafeBytes(of:packet.data){Array($0.prefix(Int(packet.length)))}
   emit(["event":"midi","origin":origin,"bytes":data,"host_timestamp":packet.timeStamp])
   ptr=UnsafePointer(MIDIPacketNext(ptr))
  }
 }
}
checked(MIDIClientCreateWithBlock("Pocket Shuttle" as CFString,&client) { n in
 emit(["event":"topology","message_id":n.pointee.messageID.rawValue])
},"client")
checked(MIDISourceCreate(client,"Pocket Shuttle" as CFString,&source),"source")
checked(MIDIDestinationCreateWithBlock(client,"Pocket Shuttle Feedback" as CFString,&destination) { p,_ in packets(p,"serato_feedback") },"destination")
for endpoint in [source,destination] {
 MIDIObjectSetStringProperty(endpoint,kMIDIPropertyManufacturer,"Pocket" as CFString)
 MIDIObjectSetStringProperty(endpoint,kMIDIPropertyModel,"Shuttle" as CFString)
}
checked(MIDIInputPortCreateWithBlock(client,"FLX4 observer" as CFString,&input) { p,_ in packets(p,"flx4") },"input")
var sources:[[String:Any]]=[]
for i in 0..<MIDIGetNumberOfSources() {
 let endpoint=MIDIGetSource(i), label=name(endpoint)
 sources.append(["endpoint":endpoint,"name":label])
 if label.uppercased().contains("FLX4") { checked(MIDIPortConnectSource(input,endpoint,nil),"observe_flx4") }
}
var destinations:[[String:Any]]=[]
for i in 0..<MIDIGetNumberOfDestinations() { let e=MIDIGetDestination(i); destinations.append(["endpoint":e,"name":name(e)]) }
emit(["event":"ready","source":source,"destination":destination,"sources":sources,"destinations":destinations])
DispatchQueue.global(qos:.userInitiated).async {
 while let line=readLine() {
  do {
   guard let raw=line.data(using:.utf8), let object=try JSONSerialization.jsonObject(with:raw) as? [String:Any] else { throw NSError(domain:"json",code:1) }
   if object["quit"] as? Bool == true { exit(0) }
   guard let integers=object["send"] as? [Int], integers.count==3,
    (0x80...0xBF).contains(integers[0]), (0...127).contains(integers[1]), (0...127).contains(integers[2]) else { throw NSError(domain:"channel_message",code:2) }
   var list=MIDIPacketList()
   let bytes=integers.map{UInt8($0)}
   let result=withUnsafeMutablePointer(to:&list) { p -> OSStatus in
    let packet=MIDIPacketListInit(p)
    return bytes.withUnsafeBufferPointer { b in
     _ = MIDIPacketListAdd(p,MemoryLayout<MIDIPacketList>.size,packet,0,bytes.count,b.baseAddress!)
     return MIDIReceived(source,p)
    }
   }
   emit(["event":"sent","bytes":integers,"status":result,"request_id":object["request_id"] ?? NSNull()])
  } catch { emit(["event":"rejected","reason":String(describing:error)]) }
 }
 exit(0)
}
CFRunLoopRun()
