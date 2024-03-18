# Traffic-AI
![Traffic AI](https://i.imgur.com/foP5Xto.jpg)

Our project represents a groundbreaking advancement in traffic analysis and toll collection systems, leveraging cutting-edge technologies including computer vision, deep learning algorithms, and real-time data processing. Our technology is intended to improve traffic management, ensure road safety, and accelerate toll collection procedures in the dynamic traffic environment of Bangladesh.

## Key Features:

### Vehicle Detection and Classification:
 Utilizing state-of-the-art deep learning models such as YOLOv8, our system accurately detects and classifies vehicles based on their types (e.g., cars, buses, motorcycles, trucks).

### Real-time Tracking:
Implementing the DeepSORT algorithm, our system tracks vehicles seamlessly across frames, enabling continuous monitoring of their movements within the traffic flow.

### Number Plate Recognition (NPR):
Through computer vision techniques, our system can identify and track vehicle number plates, facilitating various functions including toll collection, vehicle identification, and automated fine enforcement.

### Speed Tracking:
By analyzing the movement patterns of vehicles, our system calculates their speeds in real-time, allowing authorities to identify speeding vehicles and enforce speed limits effectively.

### Traffic Flow Analysis:
Our system provides insights into traffic dynamics by tracking incoming and outgoing vehicles based on their classes. This data aids in traffic management, capacity planning, and congestion mitigation efforts.

### Automated Toll Collection:
![Toll Collection Demo](https://i.imgur.com/XJepOLA.jpg)
Leveraging NPR and real-time data processing capabilities, our system enables automated toll collection by scanning vehicle number plates as they pass through toll gates. This streamlines toll operations, reduces congestion, and minimizes manual intervention.

### Automated Fine Enforcement: 
In cases of traffic violations or unpaid tolls, our system automatically generates fines by capturing relevant vehicle data, including number plates and timestamps. This enhances enforcement efficiency and ensures compliance with traffic regulations.

### User-Friendly Interface:
The system features an intuitive interface accessible to traffic authorities, enabling them to monitor traffic conditions, view analytics, and manage toll operations efficiently.

### Technological Components:

In developing our Advanced Traffic Analysis and Automated Toll Collection System for Bangladesh, we relied on a sophisticated array of technologies and tools. Our project seamlessly integrates Python programming language with OpenCV (Open Source Computer Vision Library), YOLOv8 (You Only Look Once), and a diverse range of algorithms, including the DeepSORT algorithm. Additionally, we employed Optical Character Recognition (OCR) technology to accurately read vehicle number plates, enhancing the system's functionality.

### Custom Deep Learning Models:

To meet the specific requirements of our project within the Bangladeshi context, we meticulously trained and deployed various deep learning custom models. These models were fine-tuned using datasets comprising Bangladeshi vehicles, ensuring precise vehicle detection, classification, and tracking capabilities tailored to the unique characteristics of the region's traffic patterns.

By leveraging this comprehensive technological framework, our system achieves unparalleled accuracy and efficiency in traffic analysis, toll collection, and enforcement tasks. Through continuous refinement and adaptation, we remain committed to advancing the capabilities of our system to address the evolving challenges of traffic management in Bangladesh.

By harnessing advanced technologies, our project revolutionizes traffic management and toll collection processes in Bangladesh. It promotes road safety, reduces congestion, optimizes resource utilization, and enhances overall transportation efficiency. With real-time insights and automated functionalities, our system empowers authorities to make informed decisions and uphold traffic regulations effectively.

## Setup environment to run the project 
#### Poetry to requirements.txt
```
poetry export --without-hashes --format=requirements.txt > requirements.txt
```
#### To run this project
```
streamlit run Toll_Booth.py --server.runOnSave true
```
