from ..utilities import (
    FileBuffer,
    read_numerical_value,
    wrap,
    dif_string,
    read_string_value,
    string_to_pointer,
    concatenate_string,
)


fn loop_sort(
    inout vector_: Pointer[Pointer[UInt8]],
    inout idx: DynamicVector[Int],
    lowest: Int,
    highest: Int,
):
    if lowest < highest:
        let p = vector_[highest]
        var _i = lowest - 1
        for i in range(lowest, highest):
            if dif_string(p, vector_[i]) == 1:
                _i += 1
                let cp_a = vector_[_i]
                let cp_i = idx[_i]
                vector_.store(_i, vector_[i])
                idx[_i] = idx[i]
                vector_.store(i, cp_a)
                idx[i] = cp_i
        let cp_a = vector_[
            _i + 1 + 0
        ]  # I Don't know why but get compiler error when i dont use + 0
        let cp_i = idx[
            _i + 1 + 0
        ]  # I Don't know why but get compiler error when i dont use + 0

        vector_.store(_i + 1, vector_[highest])
        idx[_i + 1] = idx[highest]
        vector_.store(highest, cp_a)
        idx[highest] = cp_i

        loop_sort(vector_, idx, lowest, _i)
        loop_sort(vector_, idx, _i + 2, highest)


struct Tokenizer:
    var vocab: Pointer[Pointer[UInt8]]
    var vocab_scores: DTypePointer[DType.float32]
    var max_token_length: Int
    var vocab_size: Int
    var sorted_vocab: Pointer[Pointer[UInt8]]
    var sorted_indices: DynamicVector[Int]

    fn __init__(inout self, vocab_size: Int, inout buffer: FileBuffer) raises -> None:
        self.vocab_size = vocab_size

        self.max_token_length = buffer.read_value_int()

        self.vocab_scores = DTypePointer[DType.float32].alloc(self.vocab_size)
        self.vocab = Pointer[Pointer[UInt8]].alloc(self.vocab_size)

        self.sorted_vocab = Pointer[Pointer[UInt8]].alloc(0)
        self.sorted_indices = DynamicVector[Int](0)

        for i in range(0, self.vocab_size):
            self.vocab_scores.store(i, buffer.read_value_float32(1).load(0))
            let string_length = buffer.read_value_int()
            self.vocab.store(i, read_string_value(buffer, string_length))

        return None

    fn sort(inout self) -> None:
        if len(self.sorted_indices) < self.vocab_size:
            self.sorted_indices = DynamicVector[Int](self.vocab_size)
            self.sorted_vocab = Pointer[Pointer[UInt8]].alloc(self.vocab_size)
            for i in range(self.vocab_size):
                self.sorted_vocab.store(i, self.vocab[i])
                self.sorted_indices.push_back(i)

        let n = self.vocab_size
        loop_sort(self.sorted_vocab, self.sorted_indices, 0, n - 1)
        return None

    fn find(inout self, t: Pointer[UInt8]) -> Int:
        let token = wrap(t)
        let n = self.vocab_size
        if len(self.sorted_indices) < n:
            self.sort()
        var left = 0
        var right = n - 1
        while left <= right:
            let mid = left + (right - left) // 2
            let comparison = dif_string(self.sorted_vocab[mid], token)
            if comparison == 0:
                return self.sorted_indices[mid]
            if comparison < 0:
                left = mid + 1
            else:
                right = mid - 1
        return -1

    fn encode(
        inout self: Self, inout input_ids: DynamicVector[Int], string: String
    ) raises:
        byte_pr_tokenizer_encoder(input_ids, string, self)


@always_inline
fn load_tokenizer[
    nelts: Int
](inout tokenizer: Tokenizer, inout buffer: FileBuffer) raises:
    tokenizer.max_token_length = (
        buffer.data.offset(buffer.offset).bitcast[DType.uint32]().load(0).to_int()
    )
    buffer.move_offset(4)

    let length_: Int
    let score_: SIMD[DType.float32, 1]
    let string_: Pointer[UInt8]

    for i in range(0, tokenizer.vocab_size):
        # FOR EACH LOOP FRIST LOAD VOCAB INDEX NEXT LENGTH OF STRING THEN STRING IT SELF
        score_ = buffer.data.offset(buffer.offset).bitcast[DType.float32]().load(0)
        buffer.move_offset(sizeof[DType.float32]())

        tokenizer.vocab_scores.simd_store[nelts](i, score_)

        length_ = (
            buffer.data.offset(buffer.offset).bitcast[DType.uint32]().load(0).to_int()
        )
        buffer.move_offset(sizeof[Int]())

        string_ = Pointer[UInt8].alloc(length_ + 1)
        for i in range(length_):
            string_.store(i, buffer.data.load(buffer.offset))
            buffer.offset += 1

        string_.store(length_, 0)
        tokenizer.vocab.store(i, string_)
    return None


@always_inline
fn byte_pr_tokenizer_encoder(
    inout input_ids: DynamicVector[Int], string: String, inout tokenizer: Tokenizer
) raises:
    for position in range(len(string)):
        let char = string_to_pointer(string[position])
        let _id = tokenizer.find(char)
        if _id == -1:
            print("You will recive an Error  !")
            return
        input_ids.push_back(_id)
    while True:
        var h_score: SIMD[DType.float32, 1] = SIMD[DType.float32, 1](-1e10)
        var b_i: Int = -1
        var b_x: Int = -1

        for i in range(len(input_ids) - 1):
            let c_string = concatenate_string(
                tokenizer.vocab[input_ids[i]], tokenizer.vocab[input_ids[i + 1]]
            )
            let _exists: Int = tokenizer.find(c_string)
            if _exists != -1 and tokenizer.vocab_scores.load(_exists) > h_score:
                h_score = tokenizer.vocab_scores.load(_exists)
                b_i = _exists
                b_x = i
        if b_x == -1:
            break

        input_ids[b_x] = b_i

        var new_input_ids: DynamicVector[Int] = DynamicVector[Int]()
        for i in range(0, b_x + 1):
            new_input_ids.push_back(input_ids[i])
        for i in range(b_x + 2, len(input_ids)):
            new_input_ids.push_back(input_ids[i])
        input_ids = new_input_ids
