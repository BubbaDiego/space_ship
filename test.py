import sqlite3
import pandas as pd
import numpy as np

def detect_anomalies(db_path, table_name, diff_threshold=3.0):
    """
    Connects to a SQLite database, retrieves ETH price data from the specified table,
    and flags anomalous entries based on sudden jumps in price using a z-score method.

    Parameters:
        db_path (str): Path to the SQLite database file.
        table_name (str): Name of the table containing ETH data.
        diff_threshold (float): The z-score threshold above which a price difference is considered anomalous.

    Returns:
        pd.DataFrame: A DataFrame containing the anomalous entries.
    """
    # Connect to the SQLite database
    conn = sqlite3.connect(db_path)
    
    # Query data from the table; adjust the column names if needed
    query = f"SELECT timestamp, price FROM {table_name} ORDER BY timestamp ASC"
    df = pd.read_sql_query(query, conn, parse_dates=['timestamp'])
    
    # Always good to close the connection when done
    conn.close()
    
    # Calculate the difference between consecutive price entries
    df['price_diff'] = df['price'].diff()
    
    # Flag any entries where the price is missing or negative
    df['missing_or_negative'] = df['price'].isna() | (df['price'] < 0)
    
    # Compute statistics for the price differences (ignoring the first NaN)
    diff_mean = df['price_diff'].iloc[1:].mean()
    diff_std = df['price_diff'].iloc[1:].std()
    
    # Avoid division by zero if there's no variation
    if diff_std == 0:
        print("Standard deviation is zero; no variation detected in price differences.")
        return pd.DataFrame()
    
    # Calculate the z-score for each price difference
    df['z_score'] = (df['price_diff'] - diff_mean) / diff_std
    
    # Define the condition for an anomaly
    anomaly_condition = (df['z_score'].abs() > diff_threshold) | (df['missing_or_negative'])
    anomalies = df[anomaly_condition]
    
    return anomalies

if __name__ == "__main__":
    # Update these parameters to match your database configuration
    db_path = "eth_data.db"
    table_name = "eth_prices"
    
    # You can adjust the diff_threshold if needed; default is 3.0 (3 standard deviations)
    anomalies = detect_anomalies(db_path, table_name, diff_threshold=3.0)
    
    if not anomalies.empty:
        print("Anomalous entries found:")
        print(anomalies[['timestamp', 'price', 'price_diff', 'z_score']])
    else:
        print("No anomalies detected.")
