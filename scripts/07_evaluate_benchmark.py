"""
07_evaluate_benchmark.py
------------------------
Computes global quality metrics (background CV, Shannon entropy, Laplacian variance sharpness,
and Sobel edge contrast) on assembled full-resolution stitched image mosaics to select
the optimal illumination correction and registration methods. Generates CSV, HTML,
and LaTeX reports summarizing the results.
"""

import argparse
import sys
import json
import subprocess
import numpy as np
import tifffile
from pathlib import Path
from tqdm import tqdm
from scipy.ndimage import variance
from collections import defaultdict
import datetime

import xml.etree.ElementTree as ET

# Attempt imports, auto-install missing
try:
    import pandas as pd
    from skimage.filters import threshold_otsu, sobel
    from skimage.measure import shannon_entropy
    import cv2
except ImportError:
    print("Installing evaluation dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "scikit-image", "opencv-python"])
    import pandas as pd
    from skimage.filters import threshold_otsu, sobel
    from skimage.measure import shannon_entropy
    import cv2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "01.Results"
RAW_DATA = PROJECT_ROOT / "00.RawData"
REPORT_DIR = RESULTS_DIR / "Report"

def extract_metadata(xml_path: Path):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    namespaces = {'v': 'http://www.zeiss.com/AXIS/Video'}
    
    meta = {
        "lens": "Unknown Objective",
        "fluor": "Alexa Fluor 555 (default)",
        "camera": "Unknown Camera",
        "scaling_x": 0.0,
        "scaling_y": 0.0
    }
    
    try:
        scaling = root.find(".//v:Scaling", namespaces)
        if scaling:
            for item in scaling.findall("v:Items/v:Distance", namespaces):
                if item.get("Id") == "X": meta["scaling_x"] = float(item.find("v:Value", namespaces).text)
                if item.get("Id") == "Y": meta["scaling_y"] = float(item.find("v:Value", namespaces).text)
                
        # Dig out objective and camera
        hardware = root.findall(".//v:HardwareSetting", namespaces)
        for hw in hardware:
            if hw.get("Name") == "Objective":
                meta["lens"] = hw.find(".//v:Value", namespaces).text
            if hw.get("Name") == "Camera":
                meta["camera"] = hw.find(".//v:Value", namespaces).text
    except Exception:
        pass
        
    return meta

def compute_metrics(image_path: Path):
    """Computes global quality metrics directly on the assembled multi-gigabyte canvas"""
    try:
        img = tifffile.imread(str(image_path))
        if img.ndim > 2:
            img = np.squeeze(img)
            if img.ndim > 2: img = img[..., 0]
            
        # Drop zero-padding (black edges from canvas assembly)
        valid_mask = img > 0
        valid_pixels = img[valid_mask]
        
        if len(valid_pixels) == 0:
            return {"CV": np.nan, "Entropy": np.nan, "Sharpness": np.nan, "Contrast": np.nan}
            
        # 1. Background Coefficient of Variation (CV) -> Shading Metric
        # Otsu threshold to separate bright cells from background
        try:
            thresh = threshold_otsu(valid_pixels)
            bg_pixels = valid_pixels[valid_pixels < thresh]
            bg_mean = np.mean(bg_pixels)
            bg_std = np.std(bg_pixels)
            cv = bg_std / bg_mean if bg_mean > 0 else np.nan
        except Exception:
            cv = np.nan
            
        # 2. Information Entropy -> Signal preservation
        img_8bit = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        ent = shannon_entropy(img_8bit)
        
        # 3. Variance of Laplacian -> Sharpness / Ghosting Penalty (Registration metric)
        # Blur from bad stitching reduces laplacian variance
        lap = cv2.Laplacian(img_8bit, cv2.CV_64F)
        sharpness = lap.var()
        
        # 4. Global Contrast (Sobel edges)
        edges = sobel(img_8bit, mask=valid_mask)
        contrast = np.mean(edges)
        
        return {
            "CV": cv,
            "Entropy": ent,
            "Sharpness": sharpness,
            "Contrast": contrast
        }
    except Exception as e:
        print(f"Error computing {image_path.name}: {e}")
        return {"CV": np.nan, "Entropy": np.nan, "Sharpness": np.nan, "Contrast": np.nan}


def generate_html(df, out_path, basic_meta):
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>AXIO Stitching Benchmark Report</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #121212; color: #e0e0e0; margin: 0; padding: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            h1, h2, h3 {{ color: #ffffff; border-bottom: 1px solid #333; padding-bottom: 10px; }}
            .card {{ background: #1e1e1e; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #333; }}
            th {{ background-color: #2c2c2c; }}
            .chart-container {{ position: relative; height: 400px; width: 100%; margin-top: 20px; }}
            .highlight {{ color: #4CAF50; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔬 AXIO Benchmark Interactive Report</h1>
            <p>Generated on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
            
            <div class="card">
                <h2>1. Methodology & Hardware</h2>
                <p><strong>Objective Lens:</strong> {basic_meta.get('lens', 'N/A')}</p>
                <p><strong>Camera:</strong> {basic_meta.get('camera', 'N/A')}</p>
                <p><strong>Fluorophore:</strong> {basic_meta.get('fluor', 'N/A')}</p>
                <p><strong>Pixel Scaling:</strong> {basic_meta.get('scaling_x', 'N/A')} µm/px</p>
                
                <h3>Shading Correction Algorithms</h3>
                <ul>
                    <li><strong>BaSiCPy:</strong> Utilizes sparse and low-rank decomposition to independently estimate multiplicative flat-field and additive dark-field components organically from the data cube.</li>
                    <li><strong>Global Median:</strong> Computes the Z-axis median across an unaligned stack of random field tiles, smoothed via uniform Gaussian convolution to estimate empirical flat-field roll-off.</li>
                    <li><strong>Spatial Rolling Ball:</strong> An approximated local-morphology spatial filter identifying regional background intensity independent of biological structure geometry.</li>
                </ul>
                
                <h3>Registration Engines</h3>
                <ul>
                    <li><strong>Coordinate (Stage):</strong> Naive layout based strictly on encoded mechanical Stage X/Y translations recorded in the Zeiss `.xml` metadata.</li>
                    <li><strong>Phase Correlation:</strong> Fast global Fourier-transform optimization to deduce precise sub-pixel X/Y shifts iteratively across overlapping edges, resolved with Least-Squares network graph.</li>
                    <li><strong>SIFT + RANSAC:</strong> Computer vision rigid topology logic detecting invariant features in overlaps, utilizing geometric RANSAC to exclude structural outliers prior to alignment.</li>
                </ul>
            </div>
            
            <div class="card">
                <h2>2. Statistical Results</h2>
                <div class="chart-container">
                    <canvas id="cvChart"></canvas>
                </div>
                <div class="chart-container">
                    <canvas id="sharpChart"></canvas>
                </div>
            </div>
            
            <div class="card">
                <h2>3. Raw Raw Data (Averaged)</h2>
                {df.groupby(['Correction', 'Stitching']).mean(numeric_only=True).round(4).to_html(classes="table", border=0)}
            </div>
        </div>
        
        <script>
            // Data injection
            const rawData = {df.to_json(orient='records')};
            
            // Average by correction method for CV
            const methods = ['basicpy', 'median', 'spatial'];
            const cvMeans = methods.map(m => {{
                let vals = rawData.filter(d => d.Correction === m).map(d => d.CV);
                return vals.reduce((a,b) => a+b, 0) / vals.length;
            }});
            
            new Chart(document.getElementById('cvChart'), {{
                type: 'bar',
                data: {{
                    labels: methods,
                    datasets: [{{
                        label: 'Background CV (Lower is Flatter)',
                        data: cvMeans,
                        backgroundColor: ['#e91e63', '#9c27b0', '#3f51b5']
                    }}]
                }},
                options: {{ maintainAspectRatio: false }}
            }});
            
            // Average by stitching method for Sharpness
            const stitchMethods = ['coord', 'phase', 'sift'];
            const sharpMeans = stitchMethods.map(m => {{
                let vals = rawData.filter(d => d.Stitching === m).map(d => d.Sharpness);
                return vals.reduce((a,b) => a+b, 0) / vals.length;
            }});
            
            new Chart(document.getElementById('sharpChart'), {{
                type: 'bar',
                data: {{
                    labels: stitchMethods,
                    datasets: [{{
                        label: 'Laplacian Sharpness (Higher = Less Ghosting)',
                        data: sharpMeans,
                        backgroundColor: ['#ff9800', '#4caf50', '#00bcd4']
                    }}]
                }},
                options: {{ maintainAspectRatio: false }}
            }});
        </script>
    </body>
    </html>
    """
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

def generate_latex(df, out_path, basic_meta):
    dt_str = datetime.datetime.now().strftime('%Y-%m-%d')
    tex = fr"""\documentclass[11pt,a4paper]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{geometry}}
\geometry{{margin=1in}}
\usepackage{{hyperref}}

\title{{Quantitative Benchmarking of Shading Correction and Registration Algorithms in Fluorescence Microscopy}}
\author{{Automated Pipeline Report}}
\date{{{dt_str}}}

\begin{{document}}
\maketitle

\begin{{abstract}}
We present a quantitative evaluation of three shading correction methodologies (BaSiCPy, Global Median Projection, Spatial Rolling Ball) and three image registration engines (Stage Correlation, Phase Correlation, SIFT/RANSAC) on gigapixel AXIO fluorescence microscopy mosaics. The pipeline analyzes background coefficient of variation (CV) to determine illumination uniformity, alongside Laplacian variance to penalize overlap ghosting and measure structural clarity.
\end{{abstract}}

\section{{Methodology}}
\subsection{{Imaging Architecture}}
Mosaics were acquired using a Zeiss AXIO scanner encoding spatial variables within an XML matrix. The primary hardware profile detected:
\begin{{itemize}}
    \item \textbf{{Objective Lens:}} {basic_meta.get('lens', 'N/A')}
    \item \textbf{{Camera Sensor:}} {basic_meta.get('camera', 'N/A')}
    \item \textbf{{Excitation Window:}} {basic_meta.get('fluor', 'N/A')}
    \item \textbf{{Spatial Sampling:}} {basic_meta.get('scaling_x', 'N/A')} $\mu m/pixel$
\end{{itemize}}

\subsection{{Algorithmic Permutations}}
A $3 \times 3$ Cartesian grid of processing algorithms was mapped against the raw datasets. Photometric irregularities (vignetting, dark counts) were isolated using sparse representation (BaSiCPy), stack statistics (Median), and morphological extraction (Spatial). Sub-pixel alignment was optimized using phase-domain topological graphs and feature extraction protocols (SIFT), heavily suppressing the intrinsic physical stage coordinate errors.

\section{{Quantitative Results}}
\subsection{{Global Evaluation Metrics}}
A perfectly shaded dataset will minimize the Coefficient of Variation (CV) strictly within the thresholded background. High Laplacian variance signifies highly preserved edge geometry, penalizing algorithm-induced blurring from pixel-level registration discontinuities (ghosting). 

\begin{{table}}[h]
\centering
\caption{{Averaged Matrix Metrics}}
\begin{{tabular}}{{ll rrr}}
\toprule
\textbf{{Correction}} & \textbf{{Stitching}} & \textbf{{Mean CV}} $\downarrow$ & \textbf{{Sharpness}} $\uparrow$ & \textbf{{Edge Cont.}} $\uparrow$ \\
\midrule
"""
    
    avg_df = df.groupby(['Correction', 'Stitching']).mean(numeric_only=True).reset_index()
    for _, row in avg_df.iterrows():
        tex += f"{row['Correction']} & {row['Stitching']} & {row['CV']:.4f} & {row['Sharpness']:.1f} & {row['Contrast']:.4f} \\\\\n"
        
    tex += r"""\bottomrule
\end{tabular}
\end{table}

\section{Conclusion}
The algorithmic permutations dramatically alter photometric and geometric consistency. By mapping the variance profiles horizontally, researchers can identify the mathematically optimal structure for high-fidelity downstream cellular quantification on the specific dataset.

\end{document}
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tex)

def evaluate_all():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. Grab Meta
    xml_path = RAW_DATA / "2026_04_17__RecognizedCode" / "2026_04_17__RecognizedCode_info.xml"
    basic_meta = extract_metadata(xml_path) if xml_path.exists() else {}
    
    records = []
    
    # 2. Iterate outputs and calculate
    datasets = ["0347", "RecognizedCode"]
    for dataset in datasets:
        ds_dir = RESULTS_DIR / dataset
        if not ds_dir.exists(): continue
        
        tif_files = list(ds_dir.glob("*.tif"))
        for tpath in tqdm(tif_files, desc=f"Analyzing {dataset}"):
            # Parse naming: scene0_basicpy_coord_ds4.tif or scene0_basicpy_coord.tif
            parts = tpath.stem.split("_")
            if len(parts) < 3: continue
            scene = parts[0].replace("scene", "")
            corr = parts[1]
            stitch = parts[2]
            
            # Skip downsampled previews for final formal report
            if "ds4" in parts:
                continue
                
            metrics = compute_metrics(tpath)
            
            records.append({
                "Dataset": dataset,
                "Scene": scene,
                "Correction": corr,
                "Stitching": stitch,
                **metrics
            })
            
    if not records:
        print("No full resolution TIFFs found in 01.Results! Run benchmark downsample 1 first.")
        return

    df = pd.DataFrame(records)
    csv_path = REPORT_DIR / "benchmark_statistics.csv"
    df.to_csv(csv_path, index=False)
    print(f"Saved raw data to {csv_path}")
    
    # Generate HTML
    html_path = REPORT_DIR / "benchmark_report.html"
    generate_html(df, html_path, basic_meta)
    print(f"Saved interactive report to {html_path}")
    
    # Generate LaTeX
    tex_path = REPORT_DIR / "benchmark_manuscript.tex"
    generate_latex(df, tex_path, basic_meta)
    print(f"Saved LaTeX manuscript to {tex_path}")
    
    # Attempt compilation
    try:
        print("Attempting to compile PDF using pdflatex...")
        # run twice for table refs
        subprocess.run(["pdflatex", "-interaction=nonstopmode", tex_path.name], cwd=str(REPORT_DIR), check=True, stdout=subprocess.DEVNULL)
        print(f"✓ PDF compiled successfully: {REPORT_DIR / 'benchmark_manuscript.pdf'}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️ pdflatex not found or failed. The .tex file is available for manual compilation or Overleaf.")

if __name__ == "__main__":
    evaluate_all()
