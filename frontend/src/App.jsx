import React, { useState, useEffect, useRef, useCallback } from 'react';
import axios from 'axios';
import { AlertTriangle, CheckCircle2, Download, Upload, FileDown, FileUp, Terminal, BarChart3, DollarSign, Package, TrendingDown } from 'lucide-react';
import clsx from 'clsx';

const API_URL = (import.meta.env.VITE_API_URL || 'https://uom-issues.onrender.com').replace(/\/+$/, '');
const MAX_FILE_SIZE_MB = 50;

function StartupOverlay({ show, dataLoaded, onClose }) {
  const [logs, setLogs] = useState([]);
  const [complete, setComplete] = useState(false);
  const [totalRows, setTotalRows] = useState(0);
  const [processedRows, setProcessedRows] = useState(0);
  const [dataReady, setDataReady] = useState(dataLoaded);
  const logEndRef = useRef(null);

  useEffect(() => {
    if (!show) return;

    let isCancelled = false;

    const poll = async () => {
      try {
        const res = await axios.get(`${API_URL}/startup_logs`);
        if (isCancelled) return;
        setLogs(res.data.logs || []);
        setTotalRows(res.data.total_rows || 0);
        setProcessedRows(res.data.processed_rows || 0);
        const done = res.data.complete;
        setComplete(done);
        if (done) {
          setDataReady(res.data.data_loaded);
        }
        return done;
      } catch {
        return false;
      }
    };

    // Initial fetch
    poll();

    const interval = setInterval(async () => {
      const done = await poll();
      if (done) {
        clearInterval(interval);
      }
    }, 2000);

    return () => {
      isCancelled = true;
      clearInterval(interval);
    };
  }, [show]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  // Close overlay once data is loaded
  if (!show) return null;
  if (complete && dataReady) return null;

  const percentage = totalRows > 0
    ? Math.min(100, Math.round((processedRows / totalRows) * 100))
    : 0;

  // Show "upload required" state when server is ready but no data loaded
  if (complete && !dataReady) {
    return (
      <div className="fixed inset-0 z-50 bg-gray-900 bg-opacity-95 flex items-center justify-center p-4">
        <div className="max-w-lg w-full text-center">
          <div className="text-6xl mb-6">📂</div>
          <h2 className="text-2xl font-bold text-white mb-3">No Data Loaded</h2>
          <p className="text-gray-300 mb-6 leading-relaxed">
            The server is running, but no GRN data file was found.<br />
            Please upload a CSV file to get started.
          </p>
          <div className="bg-gray-950 rounded-xl border border-gray-700 overflow-hidden shadow-2xl text-left">
            <div className="flex items-center space-x-2 px-4 py-2 bg-gray-800 border-b border-gray-700">
              <Terminal className="w-4 h-4 text-gray-400" />
              <span className="text-xs text-gray-400 font-mono">startup.log</span>
            </div>
            <div className="p-4 max-h-64 overflow-y-auto font-mono text-sm space-y-1">
              {logs.map((log, i) => (
                <div
                  key={i}
                  className={clsx(
                    "opacity-90 leading-relaxed",
                    log.includes("✓") ? "text-green-400" :
                    log.includes("⚠") ? "text-yellow-400" :
                    log.includes("✗") ? "text-red-400" :
                    "text-gray-300"
                  )}
                >
                  <span className="text-gray-500 mr-2">$</span>
                  {log}
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
          </div>
          <button
            onClick={onClose}
            className="mt-6 bg-indigo-600 hover:bg-indigo-700 text-white px-8 py-3 rounded-lg text-base font-medium transition-colors"
          >
            Got it — Show Upload Panel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 bg-gray-900 bg-opacity-95 flex items-center justify-center p-4">
      <div className="max-w-2xl w-full">
        <div className="flex items-center space-x-3 mb-4">
          <div className="w-5 h-5 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin"></div>
          <h2 className="text-lg font-semibold text-white">Loading Data...</h2>
        </div>

        {/* Progress bar */}
        <div className="mb-4">
          <div className="flex justify-between text-sm text-gray-300 mb-1.5">
            <span className="font-medium">Processing historical data</span>
            <span className="font-semibold text-indigo-400">{percentage}%</span>
          </div>
          <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
            <div
              className="h-3 rounded-full transition-all duration-500 ease-out bg-gradient-to-r from-indigo-500 to-indigo-700"
              style={{ width: `${percentage}%` }}
            />
          </div>
          <div className="flex justify-between mt-1">
            <span className="text-xs text-gray-400">
              {processedRows.toLocaleString()} / {totalRows.toLocaleString()} rows
            </span>
            {percentage > 0 && percentage < 100 && (
              <span className="text-xs text-gray-400">
                Initializing SKU profiles...
              </span>
            )}
          </div>
        </div>

        <div className="bg-gray-950 rounded-xl border border-gray-700 overflow-hidden shadow-2xl">
          <div className="flex items-center space-x-2 px-4 py-2 bg-gray-800 border-b border-gray-700">
            <Terminal className="w-4 h-4 text-gray-400" />
            <span className="text-xs text-gray-400 font-mono">startup.log</span>
          </div>
          <div className="p-4 max-h-96 overflow-y-auto font-mono text-sm space-y-1">
            {logs.length === 0 ? (
              <div className="text-gray-500 italic">Connecting to server...</div>
            ) : (
              logs.map((log, i) => (
                <div
                  key={i}
                  className={clsx(
                    "opacity-90 leading-relaxed",
                    log.includes("✓") ? "text-green-400" :
                    log.includes("⚠") ? "text-yellow-400" :
                    log.includes("✗") ? "text-red-400" :
                    "text-gray-300"
                  )}
                >
                  <span className="text-gray-500 mr-2">$</span>
                  {log}
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>
    </div>
  );
}

function UploadProgress({ progress, onDismiss }) {
  const logEndRef = useRef(null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [progress?.logs]);

  if (!progress) return null;

  const isFinished = progress.status === 'success' || progress.status === 'error';

  return (
    <div className="mt-3 bg-white rounded-lg border border-gray-200 overflow-hidden shadow-sm">
      {/* Terminal header */}
      <div className="flex items-center justify-between px-3 py-2 bg-gray-800 border-b border-gray-700">
        <div className="flex items-center space-x-2">
          <Terminal className="w-3.5 h-3.5 text-gray-400" />
          <span className="text-xs text-gray-400 font-mono">upload-process.log</span>
        </div>
        {isFinished && (
          <button
            onClick={onDismiss}
            className="text-xs text-gray-400 hover:text-white transition-colors"
          >
            ✕
          </button>
        )}
      </div>

      <div className="p-4 space-y-3">
        {/* Progress bar */}
        <div>
          <div className="flex justify-between text-sm text-gray-600 mb-1.5">
            <span className="font-medium">
              {progress.status === 'success' ? 'Processing complete' :
               progress.status === 'error' ? 'Processing failed' :
               progress.file_type === 'sales' ? 'Processing Sales Data...' :
               'Processing GRN Data...'}
            </span>
            <span className={clsx(
              "font-semibold",
              progress.status === 'success' ? "text-green-600" :
              progress.status === 'error' ? "text-red-600" :
              "text-indigo-600"
            )}>
              {progress.percentage}%
            </span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
            <div
              className={clsx(
                "h-3 rounded-full transition-all duration-500 ease-out",
                progress.status === 'success' ? "bg-green-500" :
                progress.status === 'error' ? "bg-red-500" :
                "bg-gradient-to-r from-indigo-500 to-indigo-700"
              )}
              style={{ width: `${progress.percentage}%` }}
            />
          </div>
          <div className="flex justify-between mt-1">
            <span className="text-xs text-gray-400">
              {(progress.processed_rows || 0).toLocaleString()} / {(progress.total_rows || 0).toLocaleString()} rows
            </span>
            {progress.percentage > 0 && progress.percentage < 100 && (
              <span className="text-xs text-gray-400">
                ~{Math.max(1, Math.round(((progress.total_rows || 1) - (progress.processed_rows || 0)) / Math.max(1, (progress.processed_rows || 0)) * 2))}s remaining
              </span>
            )}
          </div>
        </div>

        {/* Log viewer */}
        {progress.logs && progress.logs.length > 0 && (
          <div className="max-h-40 overflow-y-auto bg-gray-900 text-green-400 rounded-lg p-3 text-xs font-mono space-y-1">
            {progress.logs.map((log, i) => (
              <div
                key={i}
                className={clsx(
                  "opacity-90 leading-relaxed",
                  log.includes("✓") ? "text-green-400" :
                  log.includes("✗") ? "text-red-400" :
                  log.includes("⚠") ? "text-yellow-400" :
                  "text-gray-300"
                )}
              >
                <span className="text-gray-600 mr-1.5">{'>'}</span>
                {log}
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        )}

        {/* Status message */}
        {progress.message && isFinished && (
          <div className={clsx(
            "text-sm font-medium p-2 rounded",
            progress.status === 'success' ? "text-green-700 bg-green-50" :
            "text-red-700 bg-red-50"
          )}>
            {progress.message}
          </div>
        )}
      </div>
    </div>
  );
}

function App() {
  const [skuCode, setSkuCode] = useState('');
  const [inputPrice, setInputPrice] = useState('');
  
  const [systemUom, setSystemUom] = useState('');
  const [systemCf, setSystemCf] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  
  const [flashGreen, setFlashGreen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [isExportingSales, setIsExportingSales] = useState(false);
  const [isExportingLoss, setIsExportingLoss] = useState(false);
  
  const [selectedFile, setSelectedFile] = useState(null);
  const [fileType, setFileType] = useState('grn'); // 'grn', 'sales', or 'uom_master'
  const [isUploading, setIsUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState({ type: '', text: '' });
  const [uploadProgress, setUploadProgress] = useState(null);
  const [showStartupOverlay, setShowStartupOverlay] = useState(false);
  const [dataLoaded, setDataLoaded] = useState(false);
  const [salesSummary, setSalesSummary] = useState(null);
  const [isFetchingSummary, setIsFetchingSummary] = useState(false);
  const [uomMasterStatus, setUomMasterStatus] = useState(null);
  const [isCheckingUomMaster, setIsCheckingUomMaster] = useState(false);
  
  // Export report progress tracking (0-100, -1 = not started, -2 = checking)
  const [grnOutliersProgress, setGrnOutliersProgress] = useState(-2);
  const [salesOutliersProgress, setSalesOutliersProgress] = useState(-2);
  const [salesLossProgress, setSalesLossProgress] = useState(-2);
  const [grnTemplateAvailable, setGrnTemplateAvailable] = useState(false);
  const [salesTemplateAvailable, setSalesTemplateAvailable] = useState(true);

  const debounceTimeout = useRef(null);
  const pollTimeout = useRef(null);
  const exportProgressTimeout = useRef(null);

  // Check server status on mount
  useEffect(() => {
    const checkServer = async () => {
      try {
        const res = await axios.get(`${API_URL}/startup_logs`);
        if (!res.data.complete) {
          setShowStartupOverlay(true);
        } else if (!res.data.data_loaded) {
          setDataLoaded(false);
          setShowStartupOverlay(true);
        } else {
          setDataLoaded(true);
          fetchSalesSummary();
        }
      } catch {
        setShowStartupOverlay(true);
      }
      // Check UOM master status
      fetchUomMasterStatus();
    };
    checkServer();
  }, []);

  const fetchUomMasterStatus = async () => {
    try {
      setIsCheckingUomMaster(true);
      const res = await axios.get(`${API_URL}/uom_master_status`);
      setUomMasterStatus(res.data);
    } catch {
      // Silently fail
    } finally {
      setIsCheckingUomMaster(false);
    }
  };

  // Poll export progress periodically
  useEffect(() => {
    const pollExportProgress = async () => {
      try {
        const res = await axios.get(`${API_URL}/export_progress`);
        const data = res.data;
        setGrnOutliersProgress(data.grn_outliers);
        setSalesOutliersProgress(data.sales_outliers);
        setSalesLossProgress(data.sales_loss);
        setGrnTemplateAvailable(data.grn_template_available);
        setSalesTemplateAvailable(data.sales_template_available);
      } catch {
        // Silently fail
      }
    };

    // Poll on mount and during relevant periods
    pollExportProgress();
    const interval = setInterval(pollExportProgress, 5000); // every 5s

    return () => clearInterval(interval);
  }, []);

  const fetchSalesSummary = async () => {
    try {
      setIsFetchingSummary(true);
      const res = await axios.get(`${API_URL}/sales_analysis_summary`);
      setSalesSummary(res.data);
    } catch {
      // Silently fail - summary may not be available yet
    } finally {
      setIsFetchingSummary(false);
    }
  };

  const handleFileChange = (e) => {
    if (e.target.files && e.target.files[0]) {
      const file = e.target.files[0];
      
      if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
        setUploadMsg({ 
          type: 'error', 
          text: `File is too large (${(file.size / (1024 * 1024)).toFixed(1)} MB). Maximum allowed size is ${MAX_FILE_SIZE_MB} MB.` 
        });
        setSelectedFile(null);
        return;
      }
      
      setSelectedFile(file);
      setUploadMsg({ type: '', text: '' });
    }
  };

  const pollUploadStatus = useCallback((taskId) => {
    const poll = async () => {
      try {
        const response = await axios.get(`${API_URL}/upload_status/${taskId}`);
        const data = response.data;

        setUploadProgress(data);

        if (data.status === 'success' || data.status === 'error') {
          setUploadMsg({
            type: data.status === 'success' ? 'success' : 'error',
            text: data.message
          });
          setIsUploading(false);
          
          // Refresh sales summary if sales data was uploaded
          if (data.file_type === 'sales') {
            fetchSalesSummary();
          }
          return;
        }

        pollTimeout.current = setTimeout(poll, 2000);
      } catch (error) {
        console.error("Error polling upload status:", error);
        setUploadMsg({ type: 'error', text: 'Failed to check upload progress. The server may still be processing your file.' });
        setIsUploading(false);
      }
    };

    poll();
  }, []);

  const handleUpload = async () => {
    if (!selectedFile) return;
    setIsUploading(true);
    setUploadMsg({ type: '', text: '' });
    setUploadProgress(null);
    
    const formData = new FormData();
    formData.append('file', selectedFile);
    
    try {
      let response;
      
      if (fileType === 'uom_master') {
        // UOM master upload is synchronous
        response = await axios.post(`${API_URL}/upload_uom_master`, formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
        });
        
        if (response.data.status === 'success') {
          setUploadMsg({ type: 'success', text: response.data.message });
          fetchUomMasterStatus();
        } else {
          setUploadMsg({ type: 'error', text: response.data.message });
        }
        setIsUploading(false);
      } else {
        formData.append('file_type', fileType);
        response = await axios.post(`${API_URL}/upload_data`, formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
        });
        
        if (response.data.status === 'success') {
          setUploadMsg({ type: 'success', text: response.data.message });
          setIsUploading(false);
          if (fileType === 'sales') fetchSalesSummary();
        } else if (response.data.status === 'accepted') {
          setUploadMsg({ type: 'info', text: response.data.message });
          pollUploadStatus(response.data.task_id);
        } else {
          setUploadMsg({ type: 'error', text: response.data.message });
          setIsUploading(false);
        }
      }
    } catch (error) {
      console.error("Error uploading file:", error);
      setUploadMsg({ type: 'error', text: "Failed to upload file. Ensure backend is running." });
      setIsUploading(false);
    }
  };

  const handleDownloadTemplate = async () => {
    try {
      let url, filename;
      if (fileType === 'sales') {
        url = `${API_URL}/download_sales_template`;
        filename = 'sales_template.csv';
      } else if (fileType === 'uom_master') {
        url = `${API_URL}/download_uom_master_template`;
        filename = 'uom_master_template.csv';
      } else {
        url = `${API_URL}/download_template`;
        filename = 'grn_template.csv';
      }
      
      const response = await axios.get(url, {
        responseType: 'blob',
      });
      const blob = new Blob([response.data]);
      const downloadUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = downloadUrl;
      link.setAttribute('download', filename);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(downloadUrl);
    } catch (error) {
      console.error("Error downloading template:", error);
      alert("Failed to download template.");
    }
  };

  const handleExportOutliers = async () => {
    if (grnOutliersProgress !== -1 && grnOutliersProgress < 100) return; // still computing
    if (grnOutliersProgress === -1) return; // not available
    setIsExporting(true);
    try {
      const response = await axios.get(`${API_URL}/export_outliers`, {
        responseType: 'blob',
      });
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', 'outliers_report.xlsx');
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Error exporting outliers:", error);
      alert("Failed to export outliers.");
    } finally {
      setIsExporting(false);
    }
  };

  const handleExportSalesOutliers = async () => {
    if (salesOutliersProgress !== -1 && salesOutliersProgress < 100) return; // still computing
    if (salesOutliersProgress === -1) return; // not available
    setIsExportingSales(true);
    try {
      const response = await axios.get(`${API_URL}/export_sales_outliers`, {
        responseType: 'blob',
      });
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', 'sales_outliers_report.xlsx');
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Error exporting sales outliers:", error);
      alert("Failed to export sales outliers.");
    } finally {
      setIsExportingSales(false);
    }
  };

  const handleExportSalesLossSummary = async () => {
    if (salesLossProgress !== -1 && salesLossProgress < 100) return;
    if (salesLossProgress === -1) return;
    setIsExportingLoss(true);
    try {
      const response = await axios.get(`${API_URL}/export_sales_loss_summary`, {
        responseType: 'blob',
      });
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', 'sales_loss_summary.xlsx');
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Error exporting sales loss summary:", error);
      alert("Failed to export sales loss summary.");
    } finally {
      setIsExportingLoss(false);
    }
  };

  useEffect(() => {
    if (!skuCode || !inputPrice) {
      setSystemUom('');
      setSystemCf('');
      setErrorMsg('');
      setFlashGreen(false);
      return;
    }

    const fetchPrediction = async () => {
      setIsLoading(true);
      try {
        const response = await axios.post(`${API_URL}/predict_uom`, {
          sku_code: skuCode,
          input_price: parseFloat(inputPrice)
        });

        const data = response.data;
        if (data.status === 'success') {
          setSystemUom(data.uom);
          setSystemCf(data.cf);
          setErrorMsg('');
          setFlashGreen(true);
          setTimeout(() => setFlashGreen(false), 2000);
        } else {
          setSystemUom('');
          setSystemCf('');
          setErrorMsg(data.message || '⚠️ MANUAL REVIEW REQUIRED: The entered price drastically deviates from historical GRN data. Please verify your entry or escalate to a manager.');
          setFlashGreen(false);
        }
      } catch (err) {
        setSystemUom('');
        setSystemCf('');
        setErrorMsg('⚠️ MANUAL REVIEW REQUIRED: The entered price drastically deviates from historical GRN data. Please verify your entry or escalate to a manager.');
        setFlashGreen(false);
      } finally {
        setIsLoading(false);
      }
    };

    if (debounceTimeout.current) clearTimeout(debounceTimeout.current);

    debounceTimeout.current = setTimeout(() => {
      fetchPrediction();
    }, 500);

    return () => clearTimeout(debounceTimeout.current);
  }, [skuCode, inputPrice]);

  const handleDismissProgress = () => {
    setUploadProgress(null);
  };

  const formatCurrency = (value) => {
    if (value == null || value === '') return '₹0';
    return '₹' + Number(value).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };

  // Compute if export buttons should show progress or be clickable
  const isGrnOutliersComputing = grnOutliersProgress >= 0 && grnOutliersProgress < 100;
  const isGrnOutliersReady = grnOutliersProgress === 100;
  const isGrnOutliersUnavailable = grnOutliersProgress === -1;
  
  const isSalesOutliersComputing = salesOutliersProgress >= 0 && salesOutliersProgress < 100;
  const isSalesOutliersReady = salesOutliersProgress === 100;
  const isSalesOutliersUnavailable = salesOutliersProgress === -1;
  
  const isSalesLossComputing = salesLossProgress >= 0 && salesLossProgress < 100;
  const isSalesLossReady = salesLossProgress === 100;
  const isSalesLossUnavailable = salesLossProgress === -1;

  const isTemplateDisabled = fileType === 'grn' ? !grnTemplateAvailable : fileType === 'sales' ? !salesTemplateAvailable : false;

  // Check if we're still initializing export progress
  const isCheckingExports = grnOutliersProgress === -2;

  return (
    <div className="min-h-screen bg-gray-50 flex items-start justify-center p-4 pt-8">
      <StartupOverlay show={showStartupOverlay} dataLoaded={dataLoaded} onClose={() => setShowStartupOverlay(false)} />

      <div className="max-w-2xl w-full bg-white rounded-xl shadow-lg overflow-hidden border border-gray-100 space-y-0">
        
        {/* Header */}
        <div className="bg-slate-900 px-6 py-5 border-b border-gray-200">
          <div className="flex items-center justify-between mb-3">
            <h1 className="text-xl font-bold text-white tracking-wide">Smart GRN Entry</h1>
            <div className="flex items-center space-x-2">
              {isLoading && (
                <div className="w-5 h-5 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin"></div>
              )}
            </div>
          </div>
          
          {/* Export buttons row */}
          <div className="flex flex-wrap gap-2">
            <button 
              onClick={handleDownloadTemplate}
              disabled={isTemplateDisabled}
              className="flex items-center space-x-1.5 bg-slate-700 hover:bg-slate-600 disabled:bg-slate-500 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition-colors"
            >
              <FileDown className="w-3.5 h-3.5" />
              <span>{fileType === 'sales' ? 'Sales Template' : fileType === 'uom_master' ? 'UOM Master Template' : 'GRN Template'}</span>
            </button>
            <button 
              onClick={handleExportOutliers}
              disabled={isExporting || isGrnOutliersComputing || isGrnOutliersUnavailable || isCheckingExports}
              className={clsx(
                "flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors",
                isGrnOutliersComputing 
                  ? "bg-indigo-400 text-white cursor-not-allowed"
                  : isGrnOutliersReady
                    ? "bg-indigo-600 hover:bg-indigo-700 text-white"
                    : "bg-indigo-400 text-white cursor-not-allowed"
              )}
            >
              {isExporting ? (
                <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
              ) : isGrnOutliersComputing ? (
                <div className="flex items-center">
                  <Download className="w-3.5 h-3.5 mr-1" />
                  <span>GRN Outliers {grnOutliersProgress}%</span>
                </div>
              ) : (
                <>
                  <Download className="w-3.5 h-3.5" />
                  <span>GRN Outliers</span>
                </>
              )}
            </button>
            <button 
              onClick={handleExportSalesOutliers}
              disabled={isExportingSales || isSalesOutliersComputing || isSalesOutliersUnavailable || isCheckingExports}
              className={clsx(
                "flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors",
                isSalesOutliersComputing 
                  ? "bg-emerald-400 text-white cursor-not-allowed"
                  : isSalesOutliersReady
                    ? "bg-emerald-600 hover:bg-emerald-700 text-white"
                    : "bg-emerald-400 text-white cursor-not-allowed"
              )}
            >
              {isExportingSales ? (
                <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
              ) : isSalesOutliersComputing ? (
                <div className="flex items-center">
                  <Download className="w-3.5 h-3.5 mr-1" />
                  <span>Sales Outliers {salesOutliersProgress}%</span>
                </div>
              ) : (
                <>
                  <Download className="w-3.5 h-3.5" />
                  <span>Sales Outliers</span>
                </>
              )}
            </button>
            <button 
              onClick={handleExportSalesLossSummary}
              disabled={isExportingLoss || isSalesLossComputing || isSalesLossUnavailable || isCheckingExports}
              className={clsx(
                "flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors",
                isSalesLossComputing 
                  ? "bg-amber-400 text-white cursor-not-allowed"
                  : isSalesLossReady
                    ? "bg-amber-600 hover:bg-amber-700 text-white"
                    : "bg-amber-400 text-white cursor-not-allowed"
              )}
            >
              {isExportingLoss ? (
                <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
              ) : isSalesLossComputing ? (
                <div className="flex items-center">
                  <Download className="w-3.5 h-3.5 mr-1" />
                  <span>Loss Summary {salesLossProgress}%</span>
                </div>
              ) : (
                <>
                  <Download className="w-3.5 h-3.5" />
                  <span>Loss Summary</span>
                </>
              )}
            </button>
          </div>
        </div>

        {/* Upload Section */}
        <div className="bg-slate-50 px-6 py-4 border-b border-gray-200">
          {/* File Type Selector */}
          <div className="flex items-center space-x-4 mb-3">
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Data Type:</span>
            <div className="flex space-x-1 bg-gray-200 rounded-lg p-0.5">
              <button
                onClick={() => setFileType('grn')}
                className={clsx(
                  "px-3 py-1.5 rounded-md text-xs font-medium transition-all",
                  fileType === 'grn'
                    ? "bg-white text-indigo-700 shadow-sm"
                    : "text-gray-500 hover:text-gray-700"
                )}
              >
                PO / GRN Data
              </button>
              <button
                onClick={() => setFileType('sales')}
                className={clsx(
                  "px-3 py-1.5 rounded-md text-xs font-medium transition-all",
                  fileType === 'sales'
                    ? "bg-white text-emerald-700 shadow-sm"
                    : "text-gray-500 hover:text-gray-700"
                )}
              >
                Sales Data
              </button>
              <button
                onClick={() => setFileType('uom_master')}
                className={clsx(
                  "px-3 py-1.5 rounded-md text-xs font-medium transition-all",
                  fileType === 'uom_master'
                    ? "bg-white text-amber-700 shadow-sm"
                    : "text-gray-500 hover:text-gray-700"
                )}
              >
                UOM Master
              </button>
            </div>
          </div>

          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3 w-full max-w-md">
              <label className={clsx(
                "flex-1 cursor-pointer border rounded-lg px-4 py-2 text-sm transition-colors flex items-center justify-center",
                fileType === 'sales'
                  ? "bg-white border-emerald-300 hover:border-emerald-500 text-gray-600"
                  : fileType === 'uom_master'
                    ? "bg-white border-amber-300 hover:border-amber-500 text-gray-600"
                    : "bg-white border-gray-300 hover:border-indigo-500 text-gray-600"
              )}>
                <FileUp className="w-4 h-4 mr-2 text-gray-400" />
                <span className="truncate">{selectedFile ? selectedFile.name : "Select CSV File..."}</span>
                <input 
                  type="file" 
                  accept=".csv" 
                  className="hidden" 
                  onChange={handleFileChange} 
                />
              </label>
              <button
                onClick={handleUpload}
                disabled={!selectedFile || isUploading}
                className={clsx(
                  "text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center",
                  fileType === 'sales'
                    ? "bg-emerald-600 hover:bg-emerald-700 disabled:bg-emerald-400"
                    : fileType === 'uom_master'
                      ? "bg-amber-600 hover:bg-amber-700 disabled:bg-amber-400"
                      : "bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400"
                )}
              >
                {isUploading ? (
                   <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2"></div>
                ) : (
                  <Upload className="w-4 h-4 mr-2" />
                )}
                Upload
              </button>
            </div>
          </div>
          
          {/* UOM Master Status */}
          {fileType === 'uom_master' && uomMasterStatus && (
            <div className={clsx(
              "mt-3 p-3 rounded-lg text-sm flex items-center space-x-2",
              uomMasterStatus.loaded
                ? "bg-amber-50 text-amber-700 border border-amber-200"
                : "bg-gray-50 text-gray-500 border border-gray-200"
            )}>
              <Package className="w-4 h-4" />
              <span>
                {uomMasterStatus.loaded
                  ? `UOM Master loaded: ${uomMasterStatus.sku_count} SKUs`
                  : 'No UOM master loaded. Upload a UOM master CSV to define UOM→CF mappings.'}
              </span>
            </div>
          )}

          <UploadProgress progress={uploadProgress} onDismiss={handleDismissProgress} />

          {uploadMsg.text && !uploadProgress && (
            <div className={clsx(
              "mt-3 p-3 rounded-lg text-sm font-medium animate-in fade-in flex items-center",
              uploadMsg.type === 'success' ? "bg-green-50 text-green-700 border border-green-200" : 
              uploadMsg.type === 'info' ? "bg-blue-50 text-blue-700 border border-blue-200" :
              "bg-red-50 text-red-700 border border-red-200"
            )}>
              {uploadMsg.type === 'success' ? <CheckCircle2 className="w-4 h-4 mr-2" /> : 
               uploadMsg.type === 'info' ? <div className="w-4 h-4 border-2 border-blue-600 border-t-transparent rounded-full animate-spin mr-2" /> :
               <AlertTriangle className="w-4 h-4 mr-2" />}
              {uploadMsg.text}
            </div>
          )}
        </div>

        {/* Sales Analysis Summary Card */}
        {salesSummary && salesSummary.has_sales_data && (
          <div className="bg-gradient-to-r from-emerald-50 to-teal-50 px-6 py-4 border-b border-emerald-100">
            <div className="flex items-center space-x-2 mb-3">
              <BarChart3 className="w-4 h-4 text-emerald-600" />
              <h3 className="text-sm font-bold text-emerald-800 uppercase tracking-wider">Sales Data Analysis</h3>
              <button
                onClick={fetchSalesSummary}
                disabled={isFetchingSummary}
                className="ml-auto text-xs text-emerald-500 hover:text-emerald-700"
              >
                {isFetchingSummary ? '⟳' : '↻'}
              </button>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <div className="bg-white rounded-lg p-3 border border-emerald-100">
                <div className="flex items-center space-x-1.5 text-emerald-600 mb-1">
                  <Package className="w-3.5 h-3.5" />
                  <span className="text-xs font-medium">Outliers</span>
                </div>
                <p className="text-lg font-bold text-gray-800">{(salesSummary.outlier_count || 0).toLocaleString()}</p>
              </div>
              <div className="bg-white rounded-lg p-3 border border-emerald-100">
                <div className="flex items-center space-x-1.5 text-emerald-600 mb-1">
                  <TrendingDown className="w-3.5 h-3.5" />
                  <span className="text-xs font-medium">Units Lost</span>
                </div>
                <p className="text-lg font-bold text-gray-800">{(salesSummary.total_units_lost || 0).toLocaleString()}</p>
              </div>
              <div className="bg-white rounded-lg p-3 border border-emerald-100">
                <div className="flex items-center space-x-1.5 text-emerald-600 mb-1">
                  <Package className="w-3.5 h-3.5" />
                  <span className="text-xs font-medium">SKUs Affected</span>
                </div>
                <p className="text-lg font-bold text-gray-800">{(salesSummary.sku_count || 0).toLocaleString()}</p>
              </div>
              <div className="bg-white rounded-lg p-3 border border-emerald-100">
                <div className="flex items-center space-x-1.5 text-emerald-600 mb-1">
                  <DollarSign className="w-3.5 h-3.5" />
                  <span className="text-xs font-medium">Sales Loss</span>
                </div>
                <p className="text-lg font-bold text-red-600">{formatCurrency(salesSummary.total_sales_loss)}</p>
              </div>
            </div>
          </div>
        )}

        {/* Form Content */}
        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            <div className="space-y-1.5">
              <label htmlFor="skuCode" className="block text-sm font-semibold text-gray-700">SKU Code</label>
              <input
                id="skuCode"
                type="text"
                value={skuCode}
                onChange={(e) => setSkuCode(e.target.value)}
                placeholder="e.g. SKU12345"
                className="w-full px-4 py-2.5 rounded-lg border border-gray-300 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 transition-shadow outline-none"
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="inputPrice" className="block text-sm font-semibold text-gray-700">Entered Price (Total)</label>
              <input
                id="inputPrice"
                type="number"
                step="0.01"
                value={inputPrice}
                onChange={(e) => setInputPrice(e.target.value)}
                placeholder="0.00"
                className="w-full px-4 py-2.5 rounded-lg border border-gray-300 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 transition-shadow outline-none"
              />
            </div>
          </div>

          <hr className="border-gray-100" />

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            <div className="space-y-1.5">
              <label htmlFor="systemUom" className="block text-sm font-semibold text-gray-500">System UOM</label>
              <input
                id="systemUom"
                type="text"
                readOnly
                value={systemUom}
                placeholder="Auto-filled"
                className={clsx(
                  "w-full px-4 py-2.5 rounded-lg border bg-gray-50 text-gray-700 font-medium transition-all duration-300 outline-none cursor-not-allowed",
                  flashGreen ? "border-green-500 ring-4 ring-green-100" : "border-gray-200"
                )}
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="systemCf" className="block text-sm font-semibold text-gray-500">System CF (Conversion Factor)</label>
              <input
                id="systemCf"
                type="text"
                readOnly
                value={systemCf}
                placeholder="Auto-filled"
                className={clsx(
                  "w-full px-4 py-2.5 rounded-lg border bg-gray-50 text-gray-700 font-medium transition-all duration-300 outline-none cursor-not-allowed",
                  flashGreen ? "border-green-500 ring-4 ring-green-100" : "border-gray-200"
                )}
              />
            </div>
          </div>

          {flashGreen && !errorMsg && (
            <div className="mt-4 flex items-center text-green-700 bg-green-50 border border-green-200 rounded-lg p-3 animate-in fade-in slide-in-from-top-2">
              <CheckCircle2 className="w-5 h-5 mr-2" />
              <span className="text-sm font-medium">UOM successfully matched with historical data.</span>
            </div>
          )}

          {errorMsg && (
            <div className="mt-6 flex bg-red-50 border-l-4 border-red-500 rounded-r-lg p-4 animate-in fade-in slide-in-from-top-2 shadow-sm">
              <AlertTriangle className="w-6 h-6 text-red-600 flex-shrink-0 mr-3" />
              <div>
                <h3 className="text-red-800 font-bold text-sm tracking-wide uppercase mb-1">Manual Review Required</h3>
                <p className="text-red-700 text-sm leading-relaxed">{errorMsg}</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;