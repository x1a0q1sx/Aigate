<template>
  <div class="media-page">
    <h1 class="page-title">媒体中心</h1>
    <p class="text-sub">图片 / 视频生成统一入口（支持 OpenAI Images API 及兼容服务商）</p>

    <!-- Tab 切换 -->
    <div class="tab-bar">
      <button :class="['tab', {active: tab==='image'}]" @click="tab='image'">图片生成</button>
      <button :class="['tab', {active: tab==='video'}]" @click="tab='video'">视频生成</button>
    </div>

    <!-- ════════════ 图片生成 ════════════ -->
    <div v-show="tab==='image'" class="panel">
      <div class="grid">
        <div class="field">
          <label>服务商</label>
          <select v-model="imgForm.provider_id" @change="onImgProviderChange">
            <option :value="null" disabled>选择服务商</option>
            <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>
        <div class="field">
          <label>模型</label>
          <select v-if="!imgManual" v-model="imgForm.model" @change="onImgModelPick">
            <option :value="''" disabled>选择模型</option>
            <option v-for="m in modelsForImgProvider" :key="m.model_id" :value="m.model_id">
              {{ m.display_name || m.model_id }}
            </option>
            <option value="__manual__">✎ 手动输入模型名…</option>
          </select>
          <input v-else v-model="imgForm.model" placeholder="如 dall-e-3 / flux.1-schnell" />
        </div>
        <div class="field full">
          <label>提示词 Prompt</label>
          <textarea v-model="imgForm.prompt" rows="3" placeholder="A serene lake at dawn, soft mist..."></textarea>
        </div>
        <div class="field">
          <label>尺寸</label>
          <select v-model="imgForm.size">
            <option>256x256</option>
            <option>512x512</option>
            <option>1024x1024</option>
            <option>1792x1024</option>
            <option>1024x1792</option>
          </select>
        </div>
        <div class="field">
          <label>张数</label>
          <input type="number" v-model.number="imgForm.n" min="1" max="10" />
        </div>
        <div class="field">
          <label>质量</label>
          <select v-model="imgForm.quality">
            <option>standard</option>
            <option>hd</option>
          </select>
        </div>
        <div class="field">
          <label>输出格式</label>
          <select v-model="imgForm.response_format">
            <option value="b64_json">Base64（默认）</option>
            <option value="url">URL</option>
          </select>
        </div>
        <div class="field" v-if="imgForm.model && imgForm.model.toLowerCase().includes('dall-e')">
          <label>风格</label>
          <select v-model="imgForm.style">
            <option :value="null">默认</option>
            <option value="vivid">Vivid</option>
            <option value="natural">Natural</option>
          </select>
        </div>
        <div class="field">
          <label>Seed（固定种子）</label>
          <input type="number" v-model.number="imgForm.seed" placeholder="随机" />
        </div>
        <div class="field full">
          <label>负面提示词（可选）</label>
          <input v-model="imgForm.negative_prompt" placeholder="不想要的元素，如 blurry, watermark" />
        </div>
        <div class="field full">
          <label>图生图 — 参考图 URL（可选）</label>
          <input v-model="imgForm.image_url" placeholder="https://...  留空则为纯文生图" />
        </div>
      </div>
      <button class="btn-gen" :disabled="imgGenerating || !imgForm.prompt || !imgForm.provider_id || !imgForm.model" @click="generateImage">
        {{ imgGenerating ? '生成中...' : '生成图片' }}
      </button>
      <span v-if="imgElapsed" class="subtle">Last took {{ imgElapsed }} ms</span>
      <span v-if="imgError" class="err-text">{{ imgError }}</span>
    </div>

    <div v-show="tab==='image' && images.length" class="results">
      <h2 class="section-title">生成结果（{{ images.length }} 张）</h2>
      <div class="gallery">
        <div v-for="(img, i) in images" :key="i" class="image-card">
          <img v-if="img.format === 'base64'" :src="'data:image/png;base64,' + img.data" :alt="'img' + i" />
          <img v-else-if="img.format === 'url'" :src="img.url" :alt="'img' + i" />
          <div class="img-meta" v-if="img.revised_prompt">Revised: {{ img.revised_prompt }}</div>
          <a v-if="img.format === 'url'" :href="img.url" target="_blank" class="dl">下载 / 打开</a>
        </div>
      </div>
    </div>

    <!-- ════════════ 视频生成 ════════════ -->
    <div v-show="tab==='video'" class="panel">
      <div class="grid">
        <div class="field">
          <label>服务商</label>
          <select v-model="vidForm.provider_id" @change="onVidProviderChange">
            <option :value="null" disabled>选择服务商</option>
            <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>
        <div class="field">
          <label>模型</label>
          <select v-if="!vidManual" v-model="vidForm.model" @change="onVidModelPick">
            <option :value="''" disabled>选择模型</option>
            <option v-for="m in modelsForVidProvider" :key="m.model_id" :value="m.model_id">
              {{ m.display_name || m.model_id }}
            </option>
            <option value="__manual__">✎ 手动输入模型名…</option>
          </select>
          <input v-else v-model="vidForm.model" placeholder="如 CogVideoX / minimax-video-01 / sora-2" />
        </div>
        <div class="field full">
          <label>提示词 Prompt</label>
          <textarea v-model="vidForm.prompt" rows="3" placeholder="A cat playing piano in a jazz bar, cinematic..."></textarea>
        </div>
        <div class="field">
          <label>时长（秒）</label>
          <input type="number" v-model.number="vidForm.duration" min="1" max="60" placeholder="5" />
        </div>
        <div class="field">
          <label>分辨率</label>
          <select v-model="vidForm.size">
            <option :value="null">默认</option>
            <option>1280x720</option>
            <option>1920x1080</option>
            <option>720x1280</option>
            <option>1024x1024</option>
          </select>
        </div>
        <div class="field">
          <label>帧率 FPS</label>
          <input type="number" v-model.number="vidForm.fps" min="1" max="60" placeholder="自动" />
        </div>
        <div class="field full">
          <label>图生视频 — 起始图 URL（可选）</label>
          <input v-model="vidForm.image_url" placeholder="https://...  留空则为纯文生视频" />
        </div>
        <div class="field full">
          <label>负面提示词（可选）</label>
          <input v-model="vidForm.negative_prompt" placeholder="不想要的元素，如 low quality, distorted" />
        </div>
        <div class="field">
          <label>Seed（固定种子）</label>
          <input type="number" v-model.number="vidForm.seed" placeholder="随机" />
        </div>
      </div>
      <button class="btn-gen" :disabled="vidGenerating || !vidForm.prompt || !vidForm.provider_id || !vidForm.model" @click="generateVideo">
        {{ vidGenerating ? '生成中（可能需要 1-5 分钟）...' : '生成视频' }}
      </button>
      <span v-if="vidElapsed" class="subtle">Last took {{ vidElapsed }} ms</span>
      <span v-if="vidError" class="err-text">{{ vidError }}</span>
    </div>

    <div v-show="tab==='video' && videos.length" class="results">
      <h2 class="section-title">生成结果（{{ videos.length }} 个视频）</h2>
      <div class="gallery video-gallery">
        <div v-for="(vid, i) in videos" :key="i" class="video-card">
          <video controls :src="vid.url" class="video-player"></video>
          <div class="img-meta" v-if="vid.duration">时长: {{ vid.duration }}s</div>
          <a :href="vid.url" target="_blank" class="dl">下载 / 打开</a>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import api from '../api.js'
export default {
  name: 'Media',
  data() {
    return {
      tab: 'image',
      providers: [],
      models: [],
      imgManual: false,
      vidManual: false,
      // 图片
      imgForm: {
        provider_id: null, model: '', prompt: '', n: 1,
        size: '1024x1024', quality: 'standard',
        response_format: 'b64_json', style: null,
        seed: null, negative_prompt: '', image_url: '',
      },
      imgGenerating: false,
      images: [],
      imgElapsed: null,
      imgError: '',
      // 视频
      vidForm: {
        provider_id: null, model: '', prompt: '',
        duration: null, size: null, fps: null, image_url: null,
        negative_prompt: '', seed: null,
      },
      vidGenerating: false,
      videos: [],
      vidElapsed: null,
      vidError: '',
    }
  },
  async mounted() {
    try {
      const [ps, ms] = await Promise.all([api.getProviders(), api.getModels()])
      this.providers = ps || []
      this.models = ms || []
    } catch (e) { console.error(e) }
  },
  computed: {
    modelsForImgProvider() {
      if (!this.imgForm.provider_id) return []
      return this.models.filter(m => m.provider_id === this.imgForm.provider_id && m.enabled !== false)
    },
    modelsForVidProvider() {
      if (!this.vidForm.provider_id) return []
      return this.models.filter(m => m.provider_id === this.vidForm.provider_id && m.enabled !== false)
    },
  },
  methods: {
    onImgProviderChange() {
      this.imgManual = false
      const list = this.modelsForImgProvider
      this.imgForm.model = list.length ? list[0].model_id : ''
    },
    onVidProviderChange() {
      this.vidManual = false
      const list = this.modelsForVidProvider
      this.vidForm.model = list.length ? list[0].model_id : ''
    },
    onImgModelPick() {
      if (this.imgForm.model === '__manual__') {
        this.imgManual = true
        this.imgForm.model = ''
      }
    },
    onVidModelPick() {
      if (this.vidForm.model === '__manual__') {
        this.vidManual = true
        this.vidForm.model = ''
      }
    },
    async generateImage() {
      this.imgGenerating = true
      this.imgError = ''
      this.images = []
      try {
        const r = await api.generateImage(this.imgForm)
        this.images = r.images || []
        this.imgElapsed = r.elapsed_ms
        if (!this.images.length) this.imgError = '上游无返回图片'
      } catch (e) {
        this.imgError = e.message
      } finally {
        this.imgGenerating = false
      }
    },
    async generateVideo() {
      this.vidGenerating = true
      this.vidError = ''
      this.videos = []
      try {
        const r = await api.generateVideo(this.vidForm)
        this.videos = r.videos || []
        this.vidElapsed = r.elapsed_ms
        if (!this.videos.length) this.vidError = '上游无返回视频'
      } catch (e) {
        this.vidError = e.message
      } finally {
        this.vidGenerating = false
      }
    },
  },
}
</script>

<style scoped>
.media-page { padding: 20px; color: var(--text-primary); }
.page-title { margin: 0 0 6px 0; font-size: 22px; font-weight: 600; }
.text-sub { color: var(--text-muted); margin: 0 0 20px 0; font-size: 14px; }

.tab-bar { display: flex; gap: 8px; margin-bottom: 16px; }
.tab {
  padding: 8px 18px; border: 1px solid var(--border-base); border-radius: 6px 6px 0 0;
  background: var(--bg-elevated); color: var(--text-muted); cursor: pointer; font-size: 14px; font-weight: 500;
  border-bottom: none;
}
.tab.active { color: var(--accent-primary, #4f46e5); border-bottom: 2px solid var(--accent-primary, #4f46e5); }

.panel { background: var(--bg-elevated); border: 1px solid var(--border-base); border-radius: 8px; padding: 16px; margin-bottom: 20px; }
.section-title { margin: 24px 0 12px; font-size: 16px; font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field.full { grid-column: 1 / -1; }
.field label { font-size: 12px; color: var(--text-muted); }
.field input, .field select, .field textarea {
  padding: 8px; border-radius: 4px; border: 1px solid var(--border-base);
  background: var(--bg-input, transparent); color: var(--text-primary); font-size: 13px;
}
.field textarea { resize: vertical; }
.btn-gen { padding: 10px 18px; background: var(--accent-primary, #4f46e5); color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 500; }
.btn-gen:disabled { opacity: .5; cursor: not-allowed; }
.subtle { color: var(--text-muted); font-size: 12px; margin-left: 12px; }
.err-text { color: var(--alert-error-text, #b91c1c); margin-left: 12px; font-size: 13px; }
.results { margin-top: 24px; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
.image-card { border: 1px solid var(--border-base); border-radius: 8px; overflow: hidden; background: var(--bg-elevated); }
.image-card img { width: 100%; height: auto; display: block; }
.img-meta { font-size: 11px; color: var(--text-muted); padding: 8px 10px; }
.dl { display: inline-block; padding: 6px 10px; color: var(--accent-primary, #4f46e5); font-size: 12px; }

.video-gallery { grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }
.video-card { border: 1px solid var(--border-base); border-radius: 8px; overflow: hidden; background: var(--bg-elevated); }
.video-player { width: 100%; display: block; max-height: 320px; background: #000; }
</style>
