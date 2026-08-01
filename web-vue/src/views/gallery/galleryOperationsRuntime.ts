import { reactive, ref, type Ref } from 'vue'

import { galleryApi, type GalleryFile, type ImageStorageStats } from '@/api/gallery'
import { genboxPushApi, type GenBoxPushBatch } from '@/api/genboxPush'
import { saveBlob } from '@/lib/downloads'
import {
  formatCleanupExpiredMessage,
  formatCleanupTargetMessage,
  formatCompressStorageMessage,
  formatMb,
} from '@/views/gallery/galleryView'
import type { PageRuntime } from '@/composables/usePageRuntime'
import { usePageQuery } from '@/composables/usePageQuery'

type ConfirmDialog = {
  ask: (options: {
    title: string
    message: string
    confirmText?: string
    cancelText?: string
  }) => Promise<boolean>
}

type Toast = {
  success: (message: string, title?: string) => void
  error: (message: string, title?: string) => void
}

type GalleryOperationsRuntimeOptions = {
  runtime: PageRuntime
  confirmDialog: ConfirmDialog
  toast: Toast
  files: Ref<GalleryFile[]>
  currentPage: Ref<number>
  storageStats: Ref<ImageStorageStats | null>
  selectedPaths: Ref<Set<string>>
  startDate: Ref<string>
  endDate: Ref<string>
  loadGallery: () => Promise<void>
  closePreviewIfPath: (path: string) => void
  closeTagEditorIfPath: (path: string) => void
  clearSelection: () => void
}

export function useGalleryOperationsRuntime(options: GalleryOperationsRuntimeOptions) {
  const isStorageModalOpen = ref(false)
  const isStorageBusy = ref(false)
  const storageActionMessage = ref('')
  const storageActionError = ref('')
  const targetFreeMb = ref('500')
  const batchBusy = ref(false)
  const activePushBatch = ref<GenBoxPushBatch | null>(null)
  let pushBatchRequestGeneration = 0
  let pushBatchRefreshInFlight = false
  const operationProgress = reactive({
    open: false,
    title: '',
    subtitle: '',
    total: 0,
    current: 0,
    statusLabel: '已处理',
    message: '',
    error: '',
    busy: false,
  })

  const storageStatsQuery = usePageQuery({
    runtime: options.runtime,
    key: 'gallery:storage',
    error: storageActionError,
    errorMessage: '刷新存储统计失败',
  })

  function resetProgress(config: {
    title: string
    subtitle: string
    total: number
    message: string
  }) {
    operationProgress.open = true
    operationProgress.title = config.title
    operationProgress.subtitle = config.subtitle
    operationProgress.total = config.total
    operationProgress.current = 0
    operationProgress.statusLabel = '已提交'
    operationProgress.message = config.message
    operationProgress.error = ''
    operationProgress.busy = true
  }

  async function refreshStorageStats(optionsOverride: { lock?: boolean; silent?: boolean } = {}) {
    if (!options.runtime.isActive.value) return
    const shouldLock = optionsOverride.lock !== false
    if (shouldLock) isStorageBusy.value = true
    if (!optionsOverride.silent) storageActionError.value = ''
    await storageStatsQuery.run(
      () => galleryApi.getStorage(),
      {
        apply: (nextStats) => {
          options.storageStats.value = nextStats
        },
        onError: (message) => {
          options.toast.error(message, '刷新失败')
        },
        onSettled: (latest) => {
          if (shouldLock && latest) isStorageBusy.value = false
        },
        silentError: optionsOverride.silent,
      },
    )
  }

  function openStorageModal() {
    isStorageModalOpen.value = true
    storageActionMessage.value = ''
    storageActionError.value = ''
    void refreshStorageStats()
  }

  function closeStorageModal() {
    if (isStorageBusy.value) return
    isStorageModalOpen.value = false
  }

  async function handleCompressStorage() {
    const confirmed = await options.confirmDialog.ask({
      title: '压缩图片',
      message: '将尝试压缩本地图片以释放空间。该操作可能需要一点时间，确定继续吗？',
      confirmText: '开始压缩',
      cancelText: '取消',
    })
    if (!confirmed) return

    isStorageBusy.value = true
    storageActionMessage.value = '正在压缩图片...'
    storageActionError.value = ''
    try {
      const result = await galleryApi.compressStorage()
      storageActionMessage.value = formatCompressStorageMessage(result)
      options.toast.success(storageActionMessage.value, '压缩完成')
      await Promise.all([refreshStorageStats({ lock: false }), options.loadGallery()])
    } catch (error: any) {
      storageActionError.value = error?.message || '压缩图片失败'
      options.toast.error(storageActionError.value, '压缩失败')
    } finally {
      isStorageBusy.value = false
    }
  }

  async function handleCleanupExpired() {
    const confirmed = await options.confirmDialog.ask({
      title: '清理过期图片',
      message: '将删除图库中已过期的图片记录和文件。此操作不可恢复，确定继续吗？',
      confirmText: '清理过期',
      cancelText: '取消',
    })
    if (!confirmed) return

    isStorageBusy.value = true
    storageActionMessage.value = '正在清理过期图片...'
    storageActionError.value = ''
    try {
      const result = await galleryApi.cleanupExpired()
      storageActionMessage.value = formatCleanupExpiredMessage(result)
      options.toast.success(storageActionMessage.value, '清理完成')
      await Promise.all([refreshStorageStats({ lock: false }), options.loadGallery()])
    } catch (error: any) {
      storageActionError.value = error?.message || '清理过期图片失败'
      options.toast.error(storageActionError.value, '清理失败')
    } finally {
      isStorageBusy.value = false
    }
  }

  async function handleCleanupToTarget(dryRun: boolean) {
    const target = Number(targetFreeMb.value)
    if (!Number.isFinite(target) || target < 1) {
      storageActionError.value = '请输入有效的目标剩余空间。'
      options.toast.error(storageActionError.value, '参数错误')
      return
    }

    const normalizedTarget = Math.floor(target)
    if (!dryRun) {
      const confirmed = await options.confirmDialog.ask({
        title: '清理到目标空间',
        message: `将从旧图片开始清理，直到磁盘剩余空间尽量达到 ${formatMb(normalizedTarget)}。此操作不可恢复，确定继续吗？`,
        confirmText: '开始清理',
        cancelText: '取消',
      })
      if (!confirmed) return
    }

    isStorageBusy.value = true
    storageActionMessage.value = dryRun ? '正在预估可清理图片...' : '正在清理到目标剩余空间...'
    storageActionError.value = ''
    try {
      const result = await galleryApi.cleanupToTarget(normalizedTarget, dryRun)
      storageActionMessage.value = formatCleanupTargetMessage(result, { dryRun, normalizedTarget })
      if (dryRun) {
        options.toast.success(storageActionMessage.value, '预估完成')
        await refreshStorageStats({ lock: false })
      } else {
        options.toast.success(storageActionMessage.value, '清理完成')
        await Promise.all([refreshStorageStats({ lock: false }), options.loadGallery()])
      }
    } catch (error: any) {
      storageActionError.value = error?.message || '按目标剩余空间清理失败'
      options.toast.error(storageActionError.value, '清理失败')
    } finally {
      isStorageBusy.value = false
    }
  }

  async function handleDelete(file: GalleryFile) {
    const confirmed = await options.confirmDialog.ask({
      title: '确认删除',
      message: `确定要删除 ${file.filename} 吗？此操作不可恢复。`,
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    batchBusy.value = true
    resetProgress({
      title: '删除图片',
      subtitle: file.filename,
      total: 1,
      message: '正在提交删除请求...',
    })
    try {
      await galleryApi.deleteFile(file.path)
      operationProgress.current = 1
      operationProgress.statusLabel = '已处理'
      operationProgress.message = '删除完成，正在刷新列表...'
      options.selectedPaths.value.delete(file.path)
      options.selectedPaths.value = new Set(options.selectedPaths.value)
      options.closePreviewIfPath(file.path)
      options.closeTagEditorIfPath(file.path)
      if (options.files.value.length === 1 && options.currentPage.value > 1) {
        options.currentPage.value -= 1
      } else {
        await options.loadGallery()
      }
      options.toast.success(`已删除 ${file.filename}`, '删除成功')
      operationProgress.message = '图片已删除'
    } catch (error: any) {
      operationProgress.error = error?.message || '删除图片失败'
      options.toast.error(operationProgress.error, '删除失败')
    } finally {
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  async function handleDeleteSelected() {
    const paths = Array.from(options.selectedPaths.value)
    if (!paths.length) return
    const confirmed = await options.confirmDialog.ask({
      title: '批量删除',
      message: `确定要删除已选择的 ${paths.length} 张图片吗？此操作不可恢复。`,
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!confirmed) return

    batchBusy.value = true
    resetProgress({
      title: '批量删除图片',
      subtitle: `已选择 ${paths.length} 张`,
      total: paths.length,
      message: '正在提交批量删除请求...',
    })
    try {
      const result = await galleryApi.deleteFiles(paths)
      operationProgress.current = Number(result.removed || 0)
      operationProgress.statusLabel = '已处理'
      operationProgress.message = '删除完成，正在刷新列表...'
      options.clearSelection()
      await options.loadGallery()
      options.toast.success(`已删除 ${Number(result.removed || 0)} 张图片。`, '删除成功')
      operationProgress.message = `已删除 ${Number(result.removed || 0)} 张图片`
    } catch (error: any) {
      operationProgress.error = error?.message || '批量删除失败'
      options.toast.error(operationProgress.error, '删除失败')
    } finally {
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  async function handleBatchDownload() {
    const paths = Array.from(options.selectedPaths.value)
    if (!paths.length) return

    batchBusy.value = true
    resetProgress({
      title: '批量下载图片',
      subtitle: `已选择 ${paths.length} 张`,
      total: paths.length,
      message: '正在打包 ZIP...',
    })
    try {
      const blob = await galleryApi.downloadZip(paths)
      operationProgress.current = paths.length
      operationProgress.statusLabel = '已处理'
      operationProgress.message = 'ZIP 已生成，正在启动下载...'
      saveBlob(blob, `images_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.zip`)
      options.toast.success(`已打包 ${paths.length} 张图片。`, '下载已开始')
      operationProgress.message = `已打包 ${paths.length} 张图片`
    } catch (error: any) {
      operationProgress.error = error?.message || '批量下载失败'
      options.toast.error(operationProgress.error, '下载失败')
    } finally {
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  function applyPushBatch(batch: GenBoxPushBatch) {
    activePushBatch.value = batch
    const progressMismatch = !batch.is_terminal && !['queued', 'sending'].includes(batch.status)
    operationProgress.open = true
    operationProgress.title = '推送到 GenBox'
    operationProgress.subtitle = `已选择 ${batch.total} 张图片；源图会保留`
    operationProgress.total = batch.total
    operationProgress.current = batch.processed
    operationProgress.statusLabel = progressMismatch
      ? '状态异常'
      : batch.failed
      ? `失败 ${batch.failed}`
      : batch.retrying
        ? `重试中 ${batch.retrying}`
      : batch.already_imported
        ? `已存在 ${batch.already_imported}`
        : '已处理'
    operationProgress.message = progressMismatch
      ? '批次状态与处理计数不一致，未将此任务视为完成。请刷新后重新查看。'
      : batch.status === 'queued'
      ? batch.retrying
        ? '部分图片会在短暂等待后自动重试；源图仍保留。'
        : '已加入推送队列。你可以继续浏览图片。'
      : batch.status === 'sending'
        ? '正在推送到 GenBox；关闭此窗口不会取消任务。'
        : batch.status === 'cancelled'
          ? '剩余未开始的图片已取消；源图仍保留。'
          : batch.failed
            ? '部分图片未完成。可仅重试失败项；源图仍保留。'
            : batch.already_imported
              ? '图片已在 GenBox 中确认存在；不会重复导入，源图仍保留。'
              : '图片已推送完成；源图仍保留。'
    operationProgress.error = progressMismatch
      ? '未完成的图片不会被视为已推送，源图仍保留。'
      : batch.failed
        ? '部分图片暂时未能推送。不会影响本地源图。'
        : ''
    operationProgress.busy = !progressMismatch && (batch.status === 'queued' || batch.status === 'sending')
    if (!operationProgress.busy) {
      batchBusy.value = false
      options.runtime.clearInterval('gallery:genbox-push-batch')
    }
  }

  async function refreshPushBatch() {
    const batchId = activePushBatch.value?.id
    if (!batchId || !options.runtime.canRun.value || pushBatchRefreshInFlight) return
    const generation = ++pushBatchRequestGeneration
    pushBatchRefreshInFlight = true
    try {
      const response = await genboxPushApi.getBatch(batchId)
      if (generation !== pushBatchRequestGeneration || activePushBatch.value?.id !== batchId) return
      applyPushBatch(response.batch)
    } catch (error: any) {
      if (generation !== pushBatchRequestGeneration || activePushBatch.value?.id !== batchId) return
      operationProgress.error = error?.message || '无法刷新 GenBox 推送进度'
      operationProgress.busy = false
      batchBusy.value = false
      options.runtime.clearInterval('gallery:genbox-push-batch')
    } finally {
      pushBatchRefreshInFlight = false
    }
  }

  function startPushBatchPolling(batchId: string) {
    options.runtime.clearInterval('gallery:genbox-push-batch')
    if (!batchId) return
    options.runtime.setInterval('gallery:genbox-push-batch', 1000, () => {
      void refreshPushBatch()
    })
  }

  async function restorePushBatch() {
    if (!options.runtime.canRun.value) return
    const generation = ++pushBatchRequestGeneration
    try {
      const response = await genboxPushApi.getLatestRecoverableBatch()
      if (generation !== pushBatchRequestGeneration) return
      if (!response.batch) return
      applyPushBatch(response.batch)
      if (response.batch.status === 'queued' || response.batch.status === 'sending') {
        startPushBatchPolling(response.batch.id)
      }
    } catch {
      // Recovery is opportunistic. A fresh batch can still be created normally.
    }
  }

  function activate() {
    void restorePushBatch()
  }

  async function handlePushSelected() {
    const paths = Array.from(options.selectedPaths.value)
    if (!paths.length) return
    const confirmed = await options.confirmDialog.ask({
      title: '推送到 GenBox',
      message: `将把已选择的 ${paths.length} 张图片发送到 GenBox。源图会保留，不会删除。确定继续吗？`,
      confirmText: '开始推送',
      cancelText: '取消',
    })
    if (!confirmed) return

    batchBusy.value = true
    const generation = ++pushBatchRequestGeneration
    resetProgress({ title: '推送到 GenBox', subtitle: `已选择 ${paths.length} 张图片`, total: paths.length, message: '正在创建可恢复的推送批次...' })
    try {
      const response = await genboxPushApi.createBatch(paths)
      if (generation !== pushBatchRequestGeneration) return
      options.clearSelection()
      applyPushBatch(response.batch)
      startPushBatchPolling(response.batch.id)
    } catch (error: any) {
      operationProgress.error = error?.message || '无法创建推送批次，源图仍保留。'
      options.toast.error(operationProgress.error, '推送失败')
      batchBusy.value = false
      operationProgress.busy = false
    }
  }

  async function handlePushDateRange() {
    const startDate = options.startDate.value.trim()
    const endDate = options.endDate.value.trim()
    if (!startDate || !endDate) {
      options.toast.error('请先选择完整的开始和结束日期。', '日期范围不完整')
      return
    }
    batchBusy.value = true
    const generation = ++pushBatchRequestGeneration
    try {
      const previewResponse = await genboxPushApi.previewBatchDateRange(startDate, endDate)
      const preview = previewResponse.preview
      if (!preview.eligible_count) {
        options.toast.error('这个日期范围没有可推送的图片。', '没有可用图片')
        return
      }
      const confirmed = await options.confirmDialog.ask({
        title: '推送日期范围',
        message: `${startDate} 到 ${endDate} 共有 ${preview.eligible_count} 张可推送图片。源图会保留，不会删除。确定创建推送批次吗？`,
        confirmText: '开始推送',
        cancelText: '取消',
      })
      if (!confirmed) return
      const response = await genboxPushApi.createBatch(preview.paths)
      if (generation !== pushBatchRequestGeneration) return
      applyPushBatch(response.batch)
      startPushBatchPolling(response.batch.id)
    } catch (error: any) {
      options.toast.error(error?.message || '无法预览这个日期范围的推送任务', '推送失败')
    } finally {
      if (!operationProgress.busy) batchBusy.value = false
    }
  }

  async function cancelPushBatch() {
    const batchId = activePushBatch.value?.id
    if (!batchId) return
    ++pushBatchRequestGeneration
    try {
      const response = await genboxPushApi.cancelBatch(batchId)
      applyPushBatch(response.batch)
    } catch (error: any) {
      options.toast.error(error?.message || '无法停止剩余推送任务', '取消失败')
    }
  }

  async function retryFailedPushBatch() {
    const batchId = activePushBatch.value?.id
    if (!batchId) return
    batchBusy.value = true
    const generation = ++pushBatchRequestGeneration
    try {
      const response = await genboxPushApi.retryFailedBatch(batchId)
      if (generation !== pushBatchRequestGeneration) return
      applyPushBatch(response.batch)
      startPushBatchPolling(batchId)
    } catch (error: any) {
      batchBusy.value = false
      options.toast.error(error?.message || '无法重新提交失败图片', '重试失败')
    }
  }

  function deactivate() {
    ++pushBatchRequestGeneration
    storageStatsQuery.invalidate()
    options.runtime.clearInterval('gallery:genbox-push-batch')
    isStorageBusy.value = false
  }

  return {
    batchBusy,
    isStorageModalOpen,
    isStorageBusy,
    storageActionMessage,
    storageActionError,
    targetFreeMb,
    operationProgress,
    refreshStorageStats,
    openStorageModal,
    closeStorageModal,
    handleCompressStorage,
    handleCleanupExpired,
    handleCleanupToTarget,
    handleDelete,
    handleDeleteSelected,
    handleBatchDownload,
    handlePushSelected,
    handlePushDateRange,
    activePushBatch,
    cancelPushBatch,
    retryFailedPushBatch,
    activate,
    deactivate,
  }
}
