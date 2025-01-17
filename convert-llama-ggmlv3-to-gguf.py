import sys, struct, math, argparse
from pathlib import Path

import numpy as np

import gguf

# Note: Does not support GGML_QKK_64
QK_K = 256
# Items here are (block size, type size)
GGML_QUANT_SIZES = {
    gguf.GGMLQuantizationType.F32  : (1, 4),
    gguf.GGMLQuantizationType.F16  : (1, 2),
    gguf.GGMLQuantizationType.Q4_0 : (32, 2 + 16),
    gguf.GGMLQuantizationType.Q4_1 : (32, 2 + 2 + 16),
    gguf.GGMLQuantizationType.Q5_0 : (32, 2 + 4 + 16),
    gguf.GGMLQuantizationType.Q5_1 : (32, 2 + 2 + 4 + 16),
    gguf.GGMLQuantizationType.Q8_0 : (32, 2 + 32),
    gguf.GGMLQuantizationType.Q8_1 : (32, 4 + 4 + 32),
    gguf.GGMLQuantizationType.Q2_K : (256, 2 + 2 + QK_K // 16 + QK_K // 4),
    gguf.GGMLQuantizationType.Q3_K : (256, 2 + QK_K // 4 + QK_K // 8 + 12),
    gguf.GGMLQuantizationType.Q4_K : (256, 2 + 2 + QK_K // 2 + 12),
    gguf.GGMLQuantizationType.Q5_K : (256, 2 + 2 + QK_K // 2 + QK_K // 8 + 12),
    gguf.GGMLQuantizationType.Q6_K : (256, 2 + QK_K // 2 + QK_K // 4 + QK_K // 16),
    gguf.GGMLQuantizationType.Q8_K : (256, 4 + QK_K + QK_K // 8),
}

class Hyperparameters:
    def __init__(self):
        self.n_vocab = self.n_embd = self.n_mult = self.n_head = self.n_layer = self.n_rot = self.ftype = 0
        self.n_ff = 0

    def set_n_ff(self, model):
        ff_tensor_idx = model.tensor_map.get(b'layers.0.feed_forward.w1.weight')
        assert ff_tensor_idx is not None, 'Missing layer 0 FF tensor'
        ff_tensor = model.tensors[ff_tensor_idx]
        self.n_ff = ff_tensor.dims[1]

    def load(self, data, offset):
        (
            self.n_vocab,
            self.n_embd,
            self.n_mult,
            self.n_head,
            self.n_layer,
            self.n_rot,
            self.ftype,
        ) = struct.unpack('<7I', data[offset:offset + (4 * 7)])
        return 4 * 7

    def __str__(self):
        return f'<Hyperparameters: n_vocab={self.n_vocab}, n_embd={self.n_embd}, n_mult={self.n_mult}, n_head={self.n_head}, n_layer={self.n_layer}, n_rot={self.n_rot}, n_ff={self.n_ff}, ftype={self.ftype}>'

class Vocab:
    def __init__(self):
        self.items = []

    def load(self, data, offset, n_vocab):
        orig_offset = offset
        for _ in range(n_vocab):
            itemlen = struct.unpack('<I', data[offset:offset + 4])[0]
            assert itemlen < 4096, 'Absurd vocab item length'
            offset += 4
            vocab = bytes(data[offset:offset + itemlen])
            offset += itemlen
            score = struct.unpack('<f', data[offset:offset + 4])[0]
            offset += 4
            self.items.append((vocab, score))
        return offset - orig_offset

class Tensor:
    def __init__(self):
        self.name = None
        self.dims = ()
        self.dtype = None
        self.start_offset = 0
        self.len_bytes = 0

    def load(self, data, offset):
        orig_offset = offset
        (n_dims, name_len, dtype) = struct.unpack('<3I', data[offset:offset + 12])
        assert n_dims >= 0 and n_dims <= 4, f'Invalid tensor dimensions {n_dims}'
        assert name_len < 4096, 'Absurd tensor name length'
        quant = GGML_QUANT_SIZES.get(dtype)
        assert quant is not None, 'Unknown tensor type'
        (blksize, tysize) = quant
        offset += 12
        self.dtype= dtype
        self.dims = struct.unpack(f'<{n_dims}I', data[offset:offset + (4 * n_dims)])
        offset += 4 * n_dims
        self.name = bytes(data[offset:offset + name_len])
        offset += name_len
        pad = ((offset + 31) & ~31) - offset
        offset += pad
        n_elems = np.prod(self.dims)
        n_bytes = (n_elems * tysize) // blksize
        self.start_offset = offset
        self.len_bytes = n_bytes
        offset += n_bytes
        # print(n_dims, name_len, dtype, self.dims, self.name, pad)
        return offset - orig_offset

class GGMLV3Model:
    def __init__(self):
        self.hyperparameters = None
        self.vocab = None
        self.tensor_map = {}
        self.tensors = []

    def validate_header(self, data, offset):
        if bytes(data[offset:offset + 4]) != b'tjgg' or struct.unpack('<I', data[offset + 4:offset + 8])[0] != 3:
            raise ValueError('Only GGJTv3 supported')
        return 8

    def load(self, data, offset):
        offset += self.validate_header(data, offset)
        hp = Hyperparameters()
        offset += hp.load(data, offset)
        vocab = Vocab()
        offset += vocab.load(data, offset, hp.n_vocab)
        tensors = []
        tensor_map = {}
        while offset < len(data):
            tensor = Tensor()
            offset += tensor.load(data, offset)
            tensor_map[tensor.name] = len(tensors)
            tensors.append(tensor)
        self.hyperparameters = hp
        self.vocab = vocab
        self.tensors = tensors
        self.tensor_map = tensor_map
        hp.set_n_ff(self)
        return offset

class GGMLToGGUF:
    def __init__(self, ggml_model, data, cfg, params_override = None, vocab_override = None):
        hp = ggml_model.hyperparameters
        self.model = ggml_model
        self.data = data
        self.cfg = cfg
        self.params_override = params_override
        self.vocab_override = vocab_override
        if params_override is not None:
            n_kv_head = params_override.n_head_kv
        else:
            if cfg.gqa == 1:
                n_kv_head = hp.n_head
            else:
                gqa = float(cfg.gqa)
                n_kv_head = None
                for x in range(1, 256):
                    if float(hp.n_head) / float(x) == gqa:
                        n_kv_head = x
                assert n_kv_head is not None, "Couldn't determine n_kv_head from GQA param"
                print(f'- Guessed n_kv_head = {n_kv_head} based on GQA {cfg.gqa}')
        self.n_kv_head = n_kv_head
        self.name_map = gguf.get_tensor_name_map(gguf.MODEL_ARCH.LLAMA, ggml_model.hyperparameters.n_layer)

    def save(self):
        print('* Preparing to save GGUF file')
        gguf_writer = gguf.GGUFWriter(self.cfg.output, gguf.MODEL_ARCH_NAMES[gguf.MODEL_ARCH.LLAMA], use_temp_file = False)
        self.add_params(gguf_writer)
        self.add_vocab(gguf_writer)
        self.add_tensors(gguf_writer)
        print("    gguf: write header")
        gguf_writer.write_header_to_file()
        print("    gguf: write metadata")
        gguf_writer.write_kv_data_to_file()
        print("    gguf: write tensors")
        gguf_writer.write_tensors_to_file()
        gguf_writer.close()

    def add_params(self, gguf_writer):
        hp = self.model.hyperparameters
        cfg = self.cfg
        desc = cfg.desc if cfg.desc is not None else 'converted from legacy GGJTv3 format'
        try:
            # Filenames aren't necessarily valid UTF8.
            name = cfg.name if cfg.name is not None else cfg.input.name
        except UnicodeDecodeError:
            name = None
        print('* Adding model parameters and KV items')
        if name is not None:
            gguf_writer.add_name(name)
        gguf_writer.add_description(desc)
        if self.params_override is not None:
            po = self.params_override
            assert po.n_embd == hp.n_embd, 'Model hyperparams mismatch'
            assert po.n_layer == hp.n_layer, 'Model hyperparams mismatch'
            assert po.n_head == hp.n_head, 'Model hyperparams mismatch'
            gguf_writer.add_context_length      (po.n_ctx)
            gguf_writer.add_embedding_length    (po.n_embd)
            gguf_writer.add_block_count         (po.n_layer)
            gguf_writer.add_feed_forward_length (po.n_ff)
            gguf_writer.add_rope_dimension_count(po.n_embd // po.n_head)
            gguf_writer.add_head_count          (po.n_head)
            gguf_writer.add_head_count_kv       (po.n_head_kv)
            gguf_writer.add_layer_norm_rms_eps  (po.f_norm_eps)
            return
        gguf_writer.add_context_length(cfg.context_length)
        gguf_writer.add_embedding_length(hp.n_embd)
        gguf_writer.add_block_count(hp.n_layer)
        gguf_writer.add_feed_forward_length(hp.n_ff)
        gguf_writer.add_rope_dimension_count(hp.n_embd // hp.n_head)
        gguf_writer.add_head_count(hp.n_head)
        gguf_writer.add_head_count_kv(self.n_kv_head)
        gguf_writer.add_layer_norm_rms_eps(float(cfg.eps))

    def add_vocab(self, gguf_writer):
        hp = self.model.hyperparameters
        gguf_writer.add_tokenizer_model('llama')
        tokens = []
        scores = []
        toktypes = []
        if self.vocab_override is not None:
            vo = self.vocab_override
            print('* Adding vocab item(s)')
            for (idx, vitem) in enumerate(vo.all_tokens()):
                if len(vitem) == 3:
                    tokens.append(vitem[0])
                    scores.append(vitem[1])
                    toktypes.append(vitem[2])
                else:
                    # Maybe try to guess the token type here?
                    tokens.append(vitem[0])
                    scores.append(vitem[1])
            assert len(tokens) == hp.n_vocab, f'Override vocab has a different number of items than hyperparameters - override = {len(tokens)} but n_vocab={hp.n_vocab}'
            gguf_writer.add_token_list(tokens)
            gguf_writer.add_token_scores(scores)
            if len(toktypes) > 0:
                gguf_writer.add_token_types(toktypes)
            return
        print(f'* Adding {hp.n_vocab} vocab item(s)')
        for (tokid, (vbytes, vscore)) in enumerate(self.model.vocab.items):
            tt = 1 # Normal
            if len(vbytes) == 0:
                tt = 3 # Control
            elif tokid >= 3 and tokid <= 258 and len(vbytes) == 1:
                hv = hex(vbytes[0])[2:].upper()
                vbytes = bytes(f'<0x{hv}>', encoding = 'UTF-8')
                tt = 6 # Byte
            else:
                vbytes = vbytes.replace(b' ', b'\xe2\x96\x81')
            toktypes.append(tt)
            tokens.append(vbytes)
            scores.append(vscore)
        gguf_writer.add_token_list(tokens)
        gguf_writer.add_token_scores(scores)
        gguf_writer.add_token_types(toktypes)

    def add_tensors(self, gguf_writer):
        nm = self.name_map
        data = self.data
        print(f'* Adding {len(self.model.tensors)} tensor(s)')
        for tensor in self.model.tensors:
            name = str(tensor.name, 'UTF-8')
            if name.endswith('.weight'):
                name = name[:-7]
                suffix = '.weight'
            elif name.endswith('.bias'):
                name = name[:-5]
                suffix = '.bias'
            mapped_name = nm.get(name)
            assert mapped_name is not None, f'Bad name {name}'
            mapped_name += suffix
            tempdims = list(tensor.dims[:])
            if len(tempdims) > 1:
                temp = tempdims[1]
                tempdims[1] = tempdims[0]
                tempdims[0] = temp
            # print(f'+ {tensor.name} | {mapped_name} {tensor.dims} :: {tempdims}')
            gguf_writer.add_tensor(mapped_name, data[tensor.start_offset:tensor.start_offset + tensor.len_bytes], raw_shape = tempdims, raw_dtype = tensor.dtype)

def handle_metadata(cfg, hp):
    import convert
    assert cfg.model_metadata_dir.is_dir(), 'Metadata dir is not a directory'
    hf_config_path   = cfg.model_metadata_dir / "config.json"
    orig_config_path = cfg.model_metadata_dir / "params.json"
    # We pass a fake model here. "original" mode will check the shapes of some
    # tensors if information is missing in the .json file: other than that, the
    # model data isn't used so this should be safe (at least for now).
    fakemodel = {
        'tok_embeddings.weight': convert.LazyTensor.__new__(convert.LazyTensor),
        'layers.0.feed_forward.w1.weight': convert.LazyTensor.__new__(convert.LazyTensor),
    }
    fakemodel['tok_embeddings.weight'].shape = [hp.n_vocab]
    fakemodel['layers.0.feed_forward.w1.weight'].shape = [hp.n_ff]
    if hf_config_path.exists():
        params = convert.Params.loadHFTransformerJson(fakemodel, hf_config_path)
    elif orig_config_path.exists():
        params = convert.Params.loadOriginalParamsJson(fakemodel, orig_config_path)
    else:
        raise ValueError('Unable to load metadata')
    vocab = convert.load_vocab(cfg.vocab_dir if cfg.vocab_dir is not None else cfg.model_metadata_dir, cfg.vocabtype)
    convert.check_vocab_size(params, vocab)
    return (params, vocab)

def handle_args():
    parser = argparse.ArgumentParser(description = 'Convert GGMLv3 models to GGUF')
    parser.add_argument('--input', '-i', type = Path, help = 'Input GGMLv3 filename')
    parser.add_argument('--output', '-o', type = Path, help ='Output GGUF filename')
    parser.add_argument('--name', help = 'Set model name')
    parser.add_argument('--desc', help = 'Set model description')
    parser.add_argument('--gqa', type = int, default = 1, help = 'grouped-query attention factor (use 8 for LLaMA2 70B)')
    parser.add_argument('--eps', default = '5.0e-06', help = 'RMS norm eps: Use 1e-6 for LLaMA1 and OpenLLaMA, use 1e-5 for LLaMA2')
    parser.add_argument('--context-length', '-c', type=int, default = 2048, help = 'Default max context length: LLaMA1 is typically 2048, LLaMA2 is typically 4096')
    parser.add_argument('--model-metadata-dir', '-m', type = Path, help ='Load HuggingFace/.pth vocab and metadata from the specified directory')
    parser.add_argument("--vocab-dir", type=Path, help="directory containing tokenizer.model, if separate from model file - only meaningful with --model-metadata-dir")
    parser.add_argument("--vocabtype", choices=["spm", "bpe"], help="vocab format - only meaningful with --model-metadata-dir and/or --vocab-dir (default: spm)", default="spm")
    return parser.parse_args()

def main():
    cfg = handle_args()
    print(f'* Using config: {cfg}')
    print('\n=== WARNING === Be aware that this conversion script is best-effort. Use a native GGUF model if possible. === WARNING ===\n')
    data = np.memmap(cfg.input, mode = 'r')
    model = GGMLV3Model()
    print('* Scanning GGML input file')
    offset = model.load(data, 0)
    print(f'* GGML model hyperparameters: {model.hyperparameters}')
    vocab_override = None
    params_override = None
    if cfg.model_metadata_dir is not None:
        (params_override, vocab_override) = handle_metadata(cfg, model.hyperparameters)
        print('!! Note: When overriding params the --gqa, --eps and --context-length options are ignored.')
        print(f'* Overriding params: {params_override}')
        print(f'* Overriding vocab: {vocab_override}')
    else:
        print('\n=== WARNING === Special tokens may not be converted correctly. Use --model-metadata-dir if possible === WARNING ===\n')
    converter = GGMLToGGUF(model, data, cfg, params_override = params_override, vocab_override = vocab_override)
    converter.save()
    print(f'* Successful completion. Output saved to: {cfg.output}')

main()
