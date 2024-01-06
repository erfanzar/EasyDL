fn string_to_pointer(string: String) -> Pointer[UInt8]:
    let _len = len(string)
    let res = Pointer[UInt8]().alloc(_len + 1)
    for i in range(_len):
        res.store(i, ord(string[i]))
    res.store(_len, 0)
    return res


fn dif_string(string_1: Pointer[UInt8], string_2: Pointer[UInt8]) -> Int:
    var J: Int = 0
    while string_1[J] != 0 and string_2[J] != 0:
        if string_1[J] < string_2[J]:
            return -1
        if string_1[J] > string_2[J]:
            return 1
        J += 1
    if string_1[J] != 0 and string_2[J] == 0:
        return 1
    if string_1[J] == 0 and string_2[J] != 0:
        return -1
    return 0

fn wrap(token: Pointer[UInt8]) -> Pointer[UInt8]:
    if dif_string(token, string_to_pointer('\\n')) == 0:
        return string_to_pointer('<0x0A>')
    if dif_string(token, string_to_pointer('\\t')) == 0:
        return string_to_pointer('<0x09>')
    if dif_string(token, string_to_pointer('\'')) == 0:
        return string_to_pointer('<0x27>')
    elif dif_string(token, string_to_pointer('\"')) == 0:
        return string_to_pointer('<0x22>')
    return token

fn concatenate_string(
    string_1: Pointer[UInt8], string_2: Pointer[UInt8]
) -> Pointer[UInt8]:
    var size_1: Int = 0
    var size_2: Int = 0
    while string_1[size_1] != 0:
        size_1 += 1
    while string_2[size_2] != 0:
        size_2 += 1
    let new_str = Pointer[UInt8].alloc(size_1 + size_2 + 1)

    memcpy[UInt8](new_str, string_1, size_1)
    memcpy[UInt8](new_str.offset(size_1), string_2, size_2)

    new_str.store(size_1 + size_2, 0)
    return new_str


fn string_ref_to_uint8(string: String) -> DynamicVector[UInt8]:
    var vc: DynamicVector[UInt8] = DynamicVector[UInt8]()
    for s in range(len(string)):
        vc.push_back(ord(string[s]))
    return vc


fn uint8_to_string_ref(uint8: DynamicVector[UInt8]) -> String:
    var vc: String = String("")

    for i in range(len(uint8)):
        vc += chr(uint8[i].to_int())
    return vc


fn string_num_to_int(string_int: Int) -> Int:
    if string_int >= ord("A"):
        return string_int - ord("A") + 10
    return string_int - ord("0")


fn print_pointer(s: Pointer[UInt8]):
    if (s[1].to_int() == ord("0")) and (s[2].to_int() == ord("x")):
        print_no_newline(
            chr(
                string_num_to_int(s[3].to_int()) * 16 + string_num_to_int(s[4].to_int())
            )
        )
        return
    var index: Int = 0
    while s[index].to_int() != 0:
        print_no_newline(chr(s[index].to_int()))
        index += 1
