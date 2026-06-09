import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { AlertTriangle, CheckCircle2, Download, Upload, FileDown, FileUp } from 'lucide-react';
import clsx from 'clsx';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

function App() {
  const [skuCode, setSkuCode] = useState('');
  const [inputPrice, setInputPrice] = useState('');
  
  const [systemUom, setSystemUom] = useState('');
  const [systemCf, setSystemCf] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  
  const [flashGreen, setFlashGreen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  
  const [selectedFile, setSelectedFile] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState({ type: '', text: '' });

  const debounceTimeout = useRef(null);

  const handleFileChange = (e) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0]);
      setUploadMsg({ type: '', text: '' });
    }
  };

  const handleUpload = async () => {
    if (!selectedFile) return;
    setIsUploading(true);
    setUploadMsg({ type: '', text: '' });
    
    const formData = new FormData();
    formData.append('file', selectedFile);
    
    try {
      const response = await axios.post(`${API_URL}/upload_data`, formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      });
      if (response.data.status === 'success') {
        setUploadMsg({ type: 'success', text: response.data.message });
      } else {
        setUploadMsg({ type: 'error', text: response.data.message });
      }
    } catch (error) {
      console.error("Error uploading file:", error);
      setUploadMsg({ type: 'error', text: "Failed to upload file. Ensure backend is running." });
    } finally {
      setIsUploading(false);
    }
  };

  const handleDownloadTemplate = async () => {
    try {
      const response = await axios.get(`${API_URL}/download_template`, {
        responseType: 'blob',
      });
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', 'grn_template.xlsx');
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Error downloading template:", error);
      alert("Failed to download template.");
    }
  };

  const handleExportOutliers = async () => {
    setIsExporting(true);
    try {
      const response = await axios.get(`${API_URL}/export_outliers`, {
        responseType: 'blob', // Important for downloading files
      });
      
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', 'outliers_report.csv');
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Error exporting outliers:", error);
      alert("Failed to export outliers. Ensure backend is running and data is loaded.");
    } finally {
      setIsExporting(false);
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
          
          setTimeout(() => setFlashGreen(false), 2000); // Flash duration
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

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
      <div className="max-w-xl w-full bg-white rounded-xl shadow-lg overflow-hidden border border-gray-100">
        
        {/* Header */}
        <div className="bg-slate-900 px-6 py-5 border-b border-gray-200 flex items-center justify-between">
          <div className="flex items-center space-x-4">
            <h1 className="text-xl font-bold text-white tracking-wide">Smart GRN Entry</h1>
            {isLoading && (
              <div className="w-5 h-5 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin"></div>
            )}
          </div>
          
          <div className="flex items-center space-x-3">
            <button 
              onClick={handleDownloadTemplate}
              className="flex items-center space-x-2 bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            >
              <FileDown className="w-4 h-4" />
              <span>Template</span>
            </button>
            <button 
              onClick={handleExportOutliers}
            disabled={isExporting}
            className="flex items-center space-x-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
          >
            {isExporting ? (
               <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
            ) : (
              <Download className="w-4 h-4" />
            )}
            <span>Export Outliers</span>
          </button>
          </div>
        </div>

        {/* Upload Section */}
        <div className="bg-slate-50 px-6 py-4 border-b border-gray-200">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3 w-full max-w-md">
              <label className="flex-1 cursor-pointer bg-white border border-gray-300 hover:border-indigo-500 rounded-lg px-4 py-2 text-sm text-gray-600 transition-colors flex items-center justify-center">
                <FileUp className="w-4 h-4 mr-2 text-gray-400" />
                <span className="truncate">{selectedFile ? selectedFile.name : "Select XLSX File..."}</span>
                <input 
                  type="file" 
                  accept=".xlsx" 
                  className="hidden" 
                  onChange={handleFileChange} 
                />
              </label>
              <button
                onClick={handleUpload}
                disabled={!selectedFile || isUploading}
                className="bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center"
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
          {uploadMsg.text && (
            <div className={clsx(
              "mt-3 p-3 rounded-lg text-sm font-medium animate-in fade-in flex items-center",
              uploadMsg.type === 'success' ? "bg-green-50 text-green-700 border border-green-200" : "bg-red-50 text-red-700 border border-red-200"
            )}>
              {uploadMsg.type === 'success' ? <CheckCircle2 className="w-4 h-4 mr-2" /> : <AlertTriangle className="w-4 h-4 mr-2" />}
              {uploadMsg.text}
            </div>
          )}
        </div>

        {/* Form Content */}
        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            {/* SKU Input */}
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

            {/* Price Input */}
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

          {/* System Outputs */}
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

          {/* Visual Feedback Alerts */}
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
