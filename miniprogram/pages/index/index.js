const { uploadDiagnosis } = require('../../services/diagnosis');
const { MAX_IMAGE_BYTES } = require('../../config');

const STATUS_LABELS = {
  suspected_disease: '疑似病害',
  healthy: '未见明显病害',
  uncertain: '暂时无法判断',
  not_tomato: '未识别到番茄'
};

Page({
  data: {
    photoPath: '', description: '', busy: false, choosing: false,
    uploadProgress: 0, uploadComplete: false, stage: 'uploading',
    photoApproved: false, rejected: false, error: '', resultView: null
  },

  onLoad() { this._alive = true; this._request = null; },

  choosePhoto() {
    if (this._alive === false || this.data.busy || this.data.choosing) return;
    this.setData({ choosing: true });
    wx.chooseMedia({
      count: 1, mediaType: ['image'], sourceType: ['album', 'camera'], sizeType: ['compressed'],
      success: (response) => {
        if (this._alive === false) return;
        const file = response.tempFiles && response.tempFiles[0];
        if (!file || !file.tempFilePath || !Number.isFinite(file.size) || file.size <= 0) {
          this.setData({ error: '无法读取这张照片，请重新选择。' });
          return;
        }
        if (file.size > MAX_IMAGE_BYTES) {
          this.setData({ error: '图片超过 8 MB，请压缩或重新拍摄后再选择。' });
          return;
        }
        this.setData({ photoPath: file.tempFilePath, photoApproved: false, rejected: false, resultView: null, error: '', uploadProgress: 0, uploadComplete: false });
      },
      fail: (error) => {
        if (this._alive === false || /cancel/i.test(String(error && error.errMsg || ''))) return;
        this.setData({ error: '暂时无法打开相册或相机，请检查权限后重试。' });
      },
      complete: () => { if (this._alive !== false) this.setData({ choosing: false }); }
    });
  },

  previewPhoto() {
    if (this._alive !== false && this.data.photoPath && this.data.photoApproved) {
      wx.previewImage({ current: this.data.photoPath, urls: [this.data.photoPath] });
    }
  },

  removePhoto() {
    if (this._alive === false || this.data.busy || this.data.choosing) return;
    this.setData({ photoPath: '', photoApproved: false, rejected: false, resultView: null, error: '', uploadProgress: 0, uploadComplete: false });
  },

  onDescriptionInput(event) {
    if (this._alive === false || this.data.busy || this.data.choosing) return;
    this.setData({ description: String(event.detail.value || '').slice(0, 500), resultView: null, photoApproved: false, rejected: false, error: '' });
  },

  async recognize() {
    if (this._alive === false || this.data.busy || this.data.choosing) return;
    if (!this.data.photoPath) { this.setData({ error: '请先选择一张番茄照片。' }); return; }
    this.setData({ busy: true, photoApproved: false, rejected: false, stage: 'uploading', error: '', resultView: null, uploadProgress: 0, uploadComplete: false });
    const token = {};
    this._request = token;
    const active = () => this._alive !== false && this._request === token;
    try {
      const request = uploadDiagnosis({
        filePath: this.data.photoPath,
        description: this.data.description,
        onStage: (stage) => {
          if (active() && ['checking', 'analysing'].includes(stage)) this.setData({ stage });
        },
        onProgress: (progress) => {
          if (active()) this.setData({ uploadProgress: progress, uploadComplete: progress === 100 });
        }
      });
      token.abort = request.abort;
      const response = await request.promise;
      if (!active()) return;
      this.setData({
        photoApproved: true,
        resultView: Object.assign({}, response.result, {
          label: STATUS_LABELS[response.result.status], notice: response.notice,
          primaryDisease: response.result.disease_candidates[0] || null,
          alternativeDiseases: response.result.disease_candidates.slice(1)
        })
      });
    } catch (error) {
      if (active() && error.code !== 'REQUEST_CANCELLED') {
        if (error.code === 'CONTENT_REJECTED') {
          this.setData({ photoPath: '', description: '', resultView: null, photoApproved: false,
            rejected: true, error: '您提交的内容含违规信息。' });
        } else {
          this.setData({ photoApproved: false, error: error.message || '本次识别未完成，请重试。' });
        }
      }
    } finally {
      if (active()) { this._request = null; this.setData({ busy: false }); }
    }
  },

  onUnload() {
    this._alive = false;
    const request = this._request;
    this._request = null;
    if (request && request.abort) request.abort();
  }
});
