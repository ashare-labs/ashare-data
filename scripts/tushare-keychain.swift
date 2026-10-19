// Exact-service Keychain helper. Secret values use stdin or a private pipe only.
import Foundation
import Security
import Darwin

let service = "ashare-data.tushare"
let account = "tushare"

func fail(_ status: OSStatus) -> Never {
    FileHandle.standardError.write(Data("keychain_osstatus=\(status)\n".utf8))
    exit(1)
}

func selector() -> [String: Any] {
    return [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrSynchronizable as String: false]
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else { fail(errSecParam) }
let operation = arguments[1]
if operation == "store" && arguments.count == 2 {
    var secret = Data()
    while secret.count <= 4096 {
        let part = FileHandle.standardInput.readData(ofLength: 4097 - secret.count)
        if part.isEmpty { break }
        secret.append(part)
    }
    guard secret.count >= 16 && secret.count <= 4096 else { fail(errSecParam) }
    var addition = selector()
    addition[kSecValueData as String] = secret
    addition[kSecAttrLabel as String] = service
    let status = SecItemAdd(addition as CFDictionary, nil)
    guard status == errSecSuccess else { fail(status) }
    FileHandle.standardOutput.write(Data("{\"stored_in_keychain\":true,\"service\":\"\(service)\",\"account\":\"\(account)\"}\n".utf8))
} else if (operation == "read" && arguments.count == 3) ||
          ((operation == "status" || operation == "authorize") && arguments.count == 2) {
    var query = selector()
    if operation != "authorize" {
        query[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIFail
    }
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    query[operation == "status" ? kSecReturnAttributes as String : kSecReturnData as String] = true
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if operation == "status" && status == errSecItemNotFound {
        FileHandle.standardOutput.write(Data("{\"configured\":false}\n".utf8))
        exit(0)
    }
    guard status == errSecSuccess else { fail(status) }
    if operation == "status" {
        FileHandle.standardOutput.write(Data("{\"configured\":true}\n".utf8))
    } else if operation == "authorize" {
        guard let secret = result as? Data, secret.count >= 16, secret.count <= 4096 else { fail(errSecParam) }
        FileHandle.standardOutput.write(Data("{\"keychain_access_authorized\":true}\n".utf8))
    } else {
        guard let descriptor = Int32(arguments[2]), descriptor >= 3,
              let secret = result as? Data, secret.count >= 16, secret.count <= 4096 else { fail(errSecParam) }
        var info = stat()
        guard fstat(descriptor, &info) == 0 && (info.st_mode & S_IFMT) == S_IFIFO else { fail(errSecParam) }
        let output = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        output.write(secret)
        output.closeFile()
    }
} else {
    fail(errSecParam)
}
